"""定向披露测试：精确整数推断、历史合并、价值最大化与字典序平局、持久化。

推断引擎（最小费用流）与独立全枚举核对器在所有小表用例上逐格对拍；
题面示例（[2,4,4]/[8,6,6]）的甲/乙客户流程原样覆盖。
"""

import tempfile
import unittest
from pathlib import Path

from blend.disclosure import (
    DisclosureOffice,
    GridInfo,
    MarginNetwork,
    brute_force_cell_values,
    cell_feasible_range,
    evaluate_request,
    grid_is_feasible,
    is_safe,
)
from blend.model import DisclosureRejected, ValidationError
from blend.slice import SliceOffice

ROWS = ("R1", "R2")
COLS = ("C1", "C2", "C3")
EXAMPLE_REAL = {
    ("R1", "C1"): 2, ("R1", "C2"): 4, ("R1", "C3"): 4,
    ("R2", "C1"): 8, ("R2", "C2"): 6, ("R2", "C3"): 6,
}
EXAMPLE_RT = {"R1": 10, "R2": 20}
EXAMPLE_CT = {"C1": 10, "C2": 10, "C3": 10}


def example_grid(protected=frozenset({("R1", "C1")}), zeros=frozenset()):
    return GridInfo(1, ROWS, COLS, EXAMPLE_RT, EXAMPLE_CT, zeros, protected)


class FlowVsBruteForceTest(unittest.TestCase):
    """流引擎与全枚举在所有小表上逐格值域一致。"""

    def _assert_match(self, grid, fixed, real_cells):
        for cell in real_cells:
            flow = cell_feasible_range(grid, fixed, cell)
            brute = brute_force_cell_values(grid, fixed, cell)
            self.assertIsNotNone(brute)
            self.assertEqual(flow, (min(brute), max(brute)))

    def test_example_grid_all_fix_patterns(self):
        grid = example_grid()
        fix_sets = [
            {},
            {("R1", "C2"): 4},
            {("R1", "C2"): 4, ("R1", "C3"): 4},
            {("R2", "C1"): 8},
            {("R1", "C2"): 4, ("R2", "C3"): 6},
            {("R2", "C2"): 6, ("R2", "C3"): 6},
        ]
        for fixed in fix_sets:
            self._assert_match(grid, fixed, EXAMPLE_REAL)

    def test_structural_zero_grid(self):
        # R1=[2,8,0]，结构零 (R1,C3)
        grid = GridInfo(
            2, ROWS, COLS, EXAMPLE_RT, {"C1": 10, "C2": 14, "C3": 6},
            frozenset({("R1", "C3")}), frozenset({("R1", "C1")}),
        )
        real = dict(EXAMPLE_REAL)
        real[("R1", "C2")] = 8
        real[("R1", "C3")] = 0
        self._assert_match(grid, {}, real)
        self.assertEqual(cell_feasible_range(grid, {}, ("R1", "C3")), (0, 0))

    def test_infeasible_fixed_margins(self):
        grid = GridInfo(
            3, ("A", "B"), ("X", "Y"),
            {"A": 3, "B": 3}, {"X": 3, "Y": 3}, frozenset(), frozenset(),
        )
        self.assertFalse(grid_is_feasible(grid, {("A", "X"): 9}))
        self.assertIsNone(brute_force_cell_values(grid, {("A", "X"): 9}, ("A", "Y")))
        self.assertIsNone(cell_feasible_range(grid, {("A", "X"): 9}, ("A", "Y")))

    def test_random_small_grids(self):
        import random

        rng = random.Random(2026)
        for _ in range(60):
            nr, nc = rng.randint(2, 3), rng.randint(2, 3)
            rr = tuple(f"R{i}" for i in range(nr))
            cc = tuple(f"C{j}" for j in range(nc))
            m = {(r, c): rng.randint(0, 3) for r in rr for c in cc}
            rtot = {r: sum(m[(r, c)] for c in cc) for r in rr}
            ctot = {c: sum(m[(r, c)] for r in rr) for c in cc}
            zeros = frozenset(rng.sample(list(m), rng.randint(0, 1)))
            for z in zeros:
                m[z] = 0
                # 重算合计以保持自洽
            rtot = {r: sum(m[(r, c)] for c in cc) for r in rr}
            ctot = {c: sum(m[(r, c)] for r in rr) for c in cc}
            grid = GridInfo(4, rr, cc, rtot, ctot, zeros, frozenset())
            fixed = {}
            for cell in rng.sample(list(m), rng.randint(0, 1)):
                if cell not in zeros:
                    fixed[cell] = m[cell]
            for cell in m:
                flow = cell_feasible_range(grid, fixed, cell)
                brute = brute_force_cell_values(grid, fixed, cell)
                if brute is None:
                    self.assertIsNone(flow)
                else:
                    self.assertEqual(flow, (min(brute), max(brute)))


class SafetyPrimitiveTest(unittest.TestCase):
    def test_protected_cell_two_values_required(self):
        grid = example_grid()
        # 无披露时保护格 R1C1 可取 0..10，安全
        self.assertTrue(is_safe(grid, {}))
        # 公开 R1C2=4 后仍有多个可能值
        self.assertTrue(is_safe(grid, {("R1", "C2"): 4}))
        # 再公开 R1C3=4，行合计 10 把 R1C1 锁成 2
        self.assertFalse(is_safe(grid, {("R1", "C2"): 4, ("R1", "C3"): 4}))
        # 点名保护格本身即不安全
        self.assertFalse(is_safe(grid, {("R1", "C1"): 2}))

    def test_zero_required_flow_and_overflow(self):
        # 全部流量被定边+结构零吸收：required_flow=0，引擎正常给唯一值域
        grid = GridInfo(
            1, ("R",), ("C1", "C2"), {"R": 5}, {"C1": 5, "C2": 0},
            frozenset({("R", "C2")}), frozenset(),
        )
        net = MarginNetwork(grid, {("R", "C1"): 5})
        self.assertEqual(net.required_flow, 0)
        self.assertTrue(net.is_feasible())
        self.assertEqual(cell_feasible_range(grid, {("R", "C1"): 5}, ("R", "C1")), (5, 5))
        self.assertEqual(cell_feasible_range(grid, {("R", "C1"): 5}, ("R", "C2")), (0, 0))
        # 定边超过合计 -> 不可行 -> 不安全（不抛异常）
        bad = GridInfo(
            2, ("A", "B"), ("X", "Y"), {"A": 3, "B": 3}, {"X": 3, "Y": 3},
            frozenset(), frozenset(),
        )
        self.assertFalse(is_safe(bad, {("A", "X"): 9}))
        self.assertFalse(grid_is_feasible(bad, {("A", "X"): 9}))

    def test_empty_optional_request_accepted_with_zero_value(self):
        grid = GridInfo(
            3, ("R1", "R2", "R3"), ("C1", "C2", "C3"),
            {r: 3 for r in ("R1", "R2", "R3")},
            {c: 3 for c in ("C1", "C2", "C3")},
            frozenset(), frozenset({("R1", "C1")}),
        )
        real = {(r, c): 1 for r in grid.rows for c in grid.cols}
        r = evaluate_request(grid, {}, [], [], real)
        self.assertTrue(r["accepted"])
        self.assertEqual(r["optional_value"], 0)
        self.assertEqual(r["granted_optional"], ())
        self.assertEqual(r["granted_required"], ())

    def test_large_grid_performance_and_integrality(self):
        # 题面上限 4x4、合计 200：流引擎毫秒级，结果为整数
        rows = tuple(f"D{i}" for i in range(4))
        cols = tuple(f"F{j}" for j in range(4))
        grid = GridInfo(
            9, rows, cols, {r: 200 for r in rows}, {c: 200 for c in cols},
            frozenset({("D0", "F3")}), frozenset({("D0", "F0")}),
        )
        lo, hi = cell_feasible_range(grid, {}, ("D1", "F1"))
        self.assertIsInstance(lo, int)
        self.assertIsInstance(hi, int)
        self.assertEqual(cell_feasible_range(grid, {}, ("D0", "F3")), (0, 0))
        # 定边锁死保护格
        self.assertEqual(
            cell_feasible_range(grid, {("D0", "F1"): 200}, ("D0", "F0")), (0, 0)
        )


class EvaluateRequestTest(unittest.TestCase):
    """纯函数请求评估：题面示例与优化规则。"""

    def test_problem_example_jia_then_yi(self):
        grid = example_grid()
        # 甲先得 R1C2=4：安全
        r1 = evaluate_request(grid, {}, [("R1", "C2")], [], EXAMPLE_REAL)
        self.assertTrue(r1["accepted"])
        self.assertEqual(r1["granted_required"], (("R1", "C2"),))
        # 甲再要 R1C3=4：拒绝（行合计暴露 R1C1=2）
        r2 = evaluate_request(
            grid, r1["granted_values"], [("R1", "C3")], [], EXAMPLE_REAL
        )
        self.assertFalse(r2["accepted"])
        self.assertEqual(r2["reason"], "unsafe")
        # 同样请求单独发给新客户乙：通过
        r3 = evaluate_request(grid, {}, [("R1", "C3")], [], EXAMPLE_REAL)
        self.assertTrue(r3["accepted"])

    def test_protected_cell_naming_always_rejected(self):
        grid = example_grid()
        r = evaluate_request(grid, {}, [("R1", "C1")], [], EXAMPLE_REAL)
        self.assertFalse(r["accepted"])
        self.assertEqual(r["reason"], "protected_cell_named")
        r2 = evaluate_request(
            grid, {}, [], [(("R1", "C1"), 9)], EXAMPLE_REAL
        )
        self.assertFalse(r2["accepted"])
        self.assertEqual(r2["reason"], "protected_cell_named")

    def test_optional_value_maximization_and_lex_tiebreak(self):
        # 3x3 网格，唯一保护格 P=(R1,C1)。实测：
        #   行内对 R1C2/R1C3 各自安全、合并把 P 锁死；
        #   列内对 R2C1/R3C1 同样互斥；两组之间互不干扰。
        rows, cols = ("R1", "R2", "R3"), ("C1", "C2", "C3")
        real = {
            ("R1", "C1"): 2, ("R1", "C2"): 3, ("R1", "C3"): 5,
            ("R2", "C1"): 4, ("R2", "C2"): 6, ("R2", "C3"): 0,
            ("R3", "C1"): 4, ("R3", "C2"): 1, ("R3", "C3"): 5,
        }
        rt = {r: sum(real[(r, c)] for c in cols) for r in rows}
        ct = {c: sum(real[(r, c)] for r in rows) for c in cols}
        grid = GridInfo(8, rows, cols, rt, ct, frozenset(),
                        frozenset({("R1", "C1")}))
        self.assertTrue(is_safe(grid, {("R1", "C2"): 3}))
        self.assertTrue(is_safe(grid, {("R1", "C3"): 5}))
        self.assertFalse(is_safe(grid, {("R1", "C2"): 3, ("R1", "C3"): 5}))
        # 价值：行内对各 5（互斥取字典序小的 R1C2），列内对各 7（互斥取 R2C1），
        # 外加两个独立安全格 R2C2、R3C3 各 1。
        optional = [
            (("R1", "C2"), 5), (("R1", "C3"), 5),
            (("R2", "C1"), 7), (("R3", "C1"), 7),
            (("R2", "C2"), 1), (("R3", "C3"), 1),
        ]
        r = evaluate_request(grid, {}, [], optional, real)
        self.assertTrue(r["accepted"])
        # 最大价值 5+7+1+1=14。含 R1C2 的安全 7 分搭档只有 R3C1（R2C1 与
        # R1C2 组合会泄露），故字典序最小最优清单为：
        self.assertEqual(r["optional_value"], 14)
        self.assertEqual(
            r["granted_optional"],
            (("R1", "C2"), ("R2", "C2"), ("R3", "C1"), ("R3", "C3")),
        )
        # 抬高 R1C3 价值后，行内对改选 R1C3（字典序让位于价值）；此时列内对
        # 取字典序更小的 R2C1（它与 R1C3 不冲突）
        optional[1] = (("R1", "C3"), 9)
        r2 = evaluate_request(grid, {}, [], optional, real)
        self.assertEqual(r2["optional_value"], 18)
        self.assertEqual(
            r2["granted_optional"],
            (("R1", "C3"), ("R2", "C1"), ("R2", "C2"), ("R3", "C3")),
        )
        self.assertIn(("R1", "C3"), r2["granted_optional"])
        self.assertNotIn(("R1", "C2"), r2["granted_optional"])

    def test_already_public_cells_not_revalued(self):
        grid = example_grid()
        history = {("R1", "C2"): 4}
        r = evaluate_request(
            grid, history, [], [(("R1", "C2"), 100)], EXAMPLE_REAL
        )
        self.assertTrue(r["accepted"])
        self.assertEqual(r["optional_value"], 0)  # 已公开不计价值
        self.assertEqual(r["granted_optional"], ())
        self.assertIn(("R1", "C2"), r["skipped"])

    def test_invalid_value_and_unknown_cell(self):
        grid = example_grid()
        with self.assertRaises(ValidationError):
            evaluate_request(grid, {}, [], [(("R1", "C2"), 0)], EXAMPLE_REAL)
        with self.assertRaises(ValidationError):
            evaluate_request(grid, {}, [("R1", "X")], [], EXAMPLE_REAL)
        with self.assertRaises(ValidationError):
            evaluate_request(
                grid, {}, [], [(("R1", "C2"), 5), (("R1", "C2"), 6)], EXAMPLE_REAL
            )


class DisclosureOfficeTest(unittest.TestCase):
    def _office_with_example_slice(self):
        office = SliceOffice(":memory:")
        # 直接构造一个与题面例同构的冻结切面（绕过仓储，专注披露逻辑）
        sid = self._make_slice(office, example_grid(), EXAMPLE_REAL)
        return office, DisclosureOffice(office), sid

    def _make_slice(self, office: SliceOffice, grid: GridInfo, real: dict) -> int:
        # 借助仓储无关的低层落库：复用 freeze 的表结构，手工写一行切面。
        # 保护格写 1 个贡献者（< k=2），其余正格写 2 个，与 get_slice 重算的
        # protected 口径保持一致。
        import json
        from datetime import datetime, timezone

        conn = office._conn
        zeros_j = json.dumps(sorted(map(list, grid.structural_zeros)))
        cur = conn.execute(
            "INSERT INTO sl_slices(boundary_id, class_rev, k, note, created_at,"
            " rows_json, cols_json, zeros_json) VALUES (?,?,?,?,?,?,?,?)",
            (0, 1, 2, "test", datetime.now(timezone.utc).isoformat(),
             json.dumps(grid.rows), json.dumps(grid.cols), zeros_j),
        )
        sid = int(cur.lastrowid)
        conn.executemany(
            "INSERT INTO sl_cells(slice_id, row_name, col_name, grams, contributors)"
            " VALUES (?,?,?,?,?)",
            [
                (
                    sid, r, c, real[(r, c)],
                    1 if (r, c) in grid.protected else (2 if real[(r, c)] else 0),
                )
                for r in grid.rows for c in grid.cols
            ],
        )
        conn.commit()
        return sid

    def test_success_failure_history_and_independent_customers(self):
        office, dl, sid = self._office_with_example_slice()
        out = dl.request_disclosure(sid, "甲", required=[("R1", "C2")])
        self.assertEqual(out.granted_values[("R1", "C2")], 4)
        # 第二次必选会泄露 -> 拒绝，历史不变
        with self.assertRaises(DisclosureRejected) as ctx:
            dl.request_disclosure(sid, "甲", required=[("R1", "C3")])
        self.assertEqual(ctx.exception.reason, "unsafe")
        self.assertEqual(dl.granted_cells(sid, "甲"), {("R1", "C2"): 4})
        # 乙独立历史，同请求可过
        out_b = dl.request_disclosure(sid, "乙", required=[("R1", "C3")])
        self.assertEqual(out_b.granted_values[("R1", "C3")], 4)
        self.assertEqual(dl.granted_cells(sid, "甲"), {("R1", "C2"): 4})
        # 请求日志记录成功与失败
        log = dl.request_log(sid)
        self.assertEqual([x["accepted"] for x in log], [True, False, True])

    def test_check_request_does_not_write(self):
        office, dl, sid = self._office_with_example_slice()
        before = len(dl.request_log(sid))
        chk = dl.check_request(sid, "甲", required=[("R1", "C1")])
        self.assertFalse(chk["accepted"])
        self.assertEqual(len(dl.request_log(sid)), before)
        self.assertEqual(dl.granted_cells(sid, "甲"), {})

    def test_customer_view_hides_support_and_covered_values(self):
        office, dl, sid = self._office_with_example_slice()
        dl.request_disclosure(sid, "甲", required=[("R1", "C2")])
        view = dl.customer_view(sid, "甲")
        # 固定合计公开
        self.assertEqual(view["row_totals"], EXAMPLE_RT)
        self.assertEqual(view["col_totals"], EXAMPLE_CT)
        # 只含获准格，不含贡献者人数或被盖格原值
        self.assertEqual(
            view["disclosed_cells"],
            [{"row": "R1", "col": "C2", "grams": 4}],
        )
        rendered = repr(view)
        self.assertNotIn("contributors", rendered)
        self.assertNotIn("('R1', 'C1')", rendered)

    def test_persistence_across_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "dl.db"
            office = SliceOffice(db)
            sid = self._make_slice(office, example_grid(), EXAMPLE_REAL)
            dl = DisclosureOffice(office)
            dl.request_disclosure(sid, "甲", required=[("R1", "C2")])
            with self.assertRaises(DisclosureRejected):
                dl.request_disclosure(sid, "甲", required=[("R1", "C3")])
            dl.close(); office.close()

            office2 = SliceOffice(db)
            dl2 = DisclosureOffice(office2)
            # 重开后历史仍在，合并推断仍然拒绝
            self.assertEqual(dl2.granted_cells(sid, "甲"), {("R1", "C2"): 4})
            with self.assertRaises(DisclosureRejected):
                dl2.request_disclosure(sid, "甲", required=[("R1", "C3")])
            # 乙仍可独立获准
            out = dl2.request_disclosure(sid, "乙", required=[("R1", "C3")])
            self.assertTrue(out.granted_optional == ())
            dl2.close(); office2.close()

    def test_two_singly_safe_requests_both_succeed_only_for_separate_customers(self):
        # 题面网格：R1C2 与 R1C3 单项各自安全、合并会泄露保护格 R1C1。
        # 同一客户不能都成功；两个不同客户各自拿一个可以。
        office = SliceOffice(":memory:")
        grid = example_grid()
        sid = self._make_slice(office, grid, EXAMPLE_REAL)
        dl = DisclosureOffice(office)
        self.assertTrue(
            dl.check_request(sid, "甲", required=[("R1", "C2")])["accepted"]
        )
        dl.request_disclosure(sid, "甲", required=[("R1", "C2")])
        with self.assertRaises(DisclosureRejected):
            dl.request_disclosure(sid, "甲", required=[("R1", "C3")])
        # 甲历史不变
        self.assertEqual(dl.granted_cells(sid, "甲"), {("R1", "C2"): 4})
        # 乙拿第二个格没问题（独立历史）
        out = dl.request_disclosure(sid, "乙", required=[("R1", "C3")])
        self.assertEqual(out.granted_values[("R1", "C3")], 4)
        self.assertEqual(dl.granted_cells(sid, "乙"), {("R1", "C3"): 4})


if __name__ == "__main__":
    unittest.main()
