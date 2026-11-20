"""冻结统计切面测试：净量聚合、贡献者计数、分类缺口、结构性零、冻结不变性。

数据全部经履约仓真实入口（confirm/issue/return/cancel）产生，
切面只消费 ``issue_facts`` 只读视图。
"""

import tempfile
import unittest
from pathlib import Path

from blend.catalog import QualityCatalog
from blend.classification import ClassificationStore
from blend.model import (
    ClassificationGapError,
    StructuralZeroViolation,
    ValidationError,
)
from blend.slice import (
    MAX_COLS,
    MAX_MARGIN,
    MAX_ROWS,
    SliceOffice,
    build_slice,
)
from blend.warehouse import IssueFact, Warehouse
from tests.factories import make_lot, make_order

VOCAB = {"c1": ("0", "1")}


class SliceFixture:
    """构造一个 2 部门 × 2 物料族的小型履约仓（内存库）。"""

    def __init__(self):
        self.cat = QualityCatalog(":memory:")
        self.lots = [
            make_lot("LA1", 100, dict(VOCAB), cost_per_g=1),
            make_lot("LA2", 100, dict(VOCAB), cost_per_g=1),
            make_lot("LB1", 100, dict(VOCAB), cost_per_g=1),
            make_lot("LB2", 100, dict(VOCAB), cost_per_g=1),
        ]
        self.cat.replace_lots(self.lots)
        self.wh = Warehouse(":memory:", self.cat)
        self.wh.register_lots(self.lots)
        self.cls = ClassificationStore(":memory:")

    def force(self, order_id: str, usage: dict) -> None:
        from blend.model import PlanResult, PlannedLot
        from blend.planner import evaluate_usage, usage_cost

        order = make_order(order_id, sum(usage.values()), dict(VOCAB), max_lots=4)
        by = {l.lot_id: l for l in self.lots}
        prod = evaluate_usage(self.lots, usage, source="forced")
        planned = tuple(
            PlannedLot(lid, g, by[lid].step_g, by[lid].cost_per_g, by[lid].fixed_fee)
            for lid, g in sorted(usage.items())
        )
        res = PlanResult(
            order_id, order.total_g, planned, prod,
            usage_cost(usage, by), sum(1 for g in usage.values() if g), "forced",
        )
        pv = self.wh.preview_external(order, res)
        self.wh.confirm_preview(pv)

    def full_classification(self) -> int:
        return self.cls.replace_classification(
            {"O1": "D1", "O2": "D1", "O3": "D2", "O4": "D2", "O5": "D2"},
            {"LA1": "FA", "LA2": "FA", "LB1": "FB", "LB2": "FB"},
        )


def build_fixture() -> SliceFixture:
    fx = SliceFixture()
    # O1/D1：LA1 分两次领 6+4=10；LB1 领 8 后全额退回（净 0，退出计数）
    fx.force("O1", {"LA1": 10, "LB1": 8})
    fx.wh.issue("O1", "LA1", 6)
    fx.wh.issue("O1", "LA1", 4)
    fx.wh.issue("O1", "LB1", 8)
    fx.wh.return_material("O1", "LB1", 8)
    # O2/D1：LA2 领 5 后部分退回 2 -> 净 3（同格第二家贡献者）
    fx.force("O2", {"LA2": 5})
    fx.wh.issue("O2", "LA2", 5)
    fx.wh.return_material("O2", "LA2", 2)
    # O3/D2：LA1 领 7、LB2 领 6（两格各唯一贡献者 => 主保护格，k=2）
    fx.force("O3", {"LA1": 7, "LB2": 6})
    fx.wh.issue("O3", "LA1", 7)
    fx.wh.issue("O3", "LB2", 6)
    # O4/D2：LA2 占 9 后取消释放，从未领出 => 不计入切面
    fx.force("O4", {"LA2": 9})
    fx.wh.cancel_order("O4")
    # O5/D2：LB1 领 4 后全额退回 => 净 0
    fx.force("O5", {"LB1": 4})
    fx.wh.issue("O5", "LB1", 4)
    fx.wh.return_material("O5", "LB1", 4)
    return fx


class FactViewTest(unittest.TestCase):
    def test_facts_only_include_issue_and_return_signed(self):
        fx = build_fixture()
        facts = fx.wh.issue_facts()
        self.assertTrue(facts)
        self.assertTrue(all(isinstance(f, IssueFact) for f in facts))
        events = {f.event for f in facts}
        self.assertEqual(events, {"ISSUE", "RETURN"})  # 无 CONFIRM/CANCEL_RELEASE
        for f in facts:
            self.assertGreater(abs(f.signed_g), 0)
            if f.event == "ISSUE":
                self.assertGreater(f.signed_g, 0)
            else:
                self.assertLess(f.signed_g, 0)
        # 边界裁剪：只取前 2 条事实
        first_two = fx.wh.issue_facts(boundary_id=facts[1].ledger_id)
        self.assertEqual(len(first_two), 2)
        self.assertEqual(first_two[-1].ledger_id, facts[1].ledger_id)
        with self.assertRaises(ValidationError):
            fx.wh.issue_facts(boundary_id=-1)

    def test_boundary_freezes_event_horizon(self):
        fx = SliceFixture()
        fx.force("O1", {"LA1": 10})
        fx.wh.issue("O1", "LA1", 4)
        b1 = fx.wh.max_ledger_id()
        fx.wh.issue("O1", "LA1", 6)  # 边界之后的新事实
        facts = fx.wh.issue_facts(b1)
        self.assertEqual(sum(f.signed_g for f in facts), 4)
        self.assertEqual(fx.wh.max_ledger_id(), b1 + 1)


class BuildSliceTest(unittest.TestCase):
    def test_nets_contributors_and_protected_cells(self):
        fx = build_fixture()
        rev = fx.full_classification()
        office = SliceOffice(":memory:")
        sid = office.freeze_from(
            fx.wh, fx.cls, k=2, structural_zeros=[("D1", "FB")]
        )
        sl = office.get_slice(sid)
        # 矩阵精确值
        self.assertEqual(sl.matrix[("D1", "FA")], 13)
        self.assertEqual(sl.matrix[("D1", "FB")], 0)
        self.assertEqual(sl.matrix[("D2", "FA")], 7)
        self.assertEqual(sl.matrix[("D2", "FB")], 6)
        # 贡献者：同一订单多次领只算一家；全退退出计数
        self.assertEqual(sl.contributors[("D1", "FA")], 2)
        self.assertEqual(sl.contributors[("D1", "FB")], 0)
        self.assertEqual(sl.contributors[("D2", "FA")], 1)
        self.assertEqual(sl.contributors[("D2", "FB")], 1)
        # 主保护格：非空且贡献者 < k；普通零格 (D1,FB) 不是保护格
        self.assertEqual(sl.protected, frozenset({("D2", "FA"), ("D2", "FB")}))
        self.assertNotIn(("D1", "FB"), sl.protected)
        # 合计
        self.assertEqual(sl.row_totals, {"D1": 13, "D2": 13})
        self.assertEqual(sl.col_totals, {"FA": 20, "FB": 6})
        self.assertEqual(sl.class_rev, rev)
        self.assertEqual(sl.k, 2)

    def test_order_contribution_detail_and_cancelled_exclusion(self):
        fx = build_fixture()
        fx.full_classification()
        office = SliceOffice(":memory:")
        sid = office.freeze_from(fx.wh, fx.cls, k=2)
        nets = {
            (o.order_id, o.row, o.col): o.net_g
            for o in office.get_order_contributions(sid)
        }
        self.assertEqual(nets[("O1", "D1", "FA")], 10)
        self.assertEqual(nets[("O2", "D1", "FA")], 3)
        self.assertEqual(nets[("O3", "D2", "FA")], 7)
        self.assertEqual(nets[("O3", "D2", "FB")], 6)
        # 净 0 的订单格不出现（O1@FB、O5@FB 全退）
        self.assertNotIn(("O1", "D1", "FB"), nets)
        self.assertNotIn(("O5", "D2", "FB"), nets)
        # 取消未领的 O4 完全不计
        self.assertFalse(any(o.order_id == "O4" for o in office.get_order_contributions(sid)))
        # 贡献者计数可由明细独立重算
        sl = office.get_slice(sid)
        for cell in sl.matrix:
            recomputed = sum(1 for o in office.get_order_contributions(sid)
                             if (o.row, o.col) == cell and o.net_g > 0)
            self.assertEqual(recomputed, sl.contributors[cell])

    def test_classification_gap_blocks_publication(self):
        fx = build_fixture()
        # 故意漏掉 O3 与 lot LB2
        fx.cls.replace_classification(
            {"O1": "D1", "O2": "D1", "O4": "D2", "O5": "D2"},
            {"LA1": "FA", "LA2": "FA", "LB1": "FB"},
        )
        office = SliceOffice(":memory:")
        with self.assertRaises(ClassificationGapError) as ctx:
            office.freeze_from(fx.wh, fx.cls, k=2)
        self.assertIn("O3", ctx.exception.missing_orders)
        self.assertIn("LB2", ctx.exception.missing_lots)
        self.assertEqual(ctx.exception.rev, 1)
        # 失败后切面库内不留任何半成品
        self.assertEqual(office.list_slices(), [])

    def test_structural_zero_violation_fails_freeze(self):
        fx = build_fixture()
        fx.full_classification()
        office = SliceOffice(":memory:")
        with self.assertRaises(StructuralZeroViolation) as ctx:
            office.freeze_from(fx.wh, fx.cls, k=2, structural_zeros=[("D2", "FB")])
        self.assertEqual(ctx.exception.violating_cells, (("D2", "FB"),))
        self.assertEqual(office.list_slices(), [])
        # 普通零格可以直接冻结，不需要声明成结构性零
        sid = office.freeze_from(fx.wh, fx.cls, k=2)
        self.assertEqual(office.get_slice(sid).structural_zeros, frozenset())

    def test_ordinary_zero_is_not_public_structural_zero(self):
        built = build_slice(
            facts=[IssueFact(1, "O1", "L1", "ISSUE", 5, "t")],
            order_departments={"O1": "D1"},
            lot_families={"L1": "F1"},
            structural_zeros=(),
            k=2,
        )
        self.assertEqual(built["matrix"][("D1", "F1")], 5)
        self.assertEqual(built["structural_zeros"], frozenset())
        # (D1,F1) 唯一贡献者 -> 保护格；没有事实的格是普通零，不保护也不结构零
        self.assertEqual(built["protected"], frozenset({("D1", "F1")}))

    def test_k_threshold_variants(self):
        fx = build_fixture()
        fx.full_classification()
        office = SliceOffice(":memory:")
        sid1 = office.freeze_from(fx.wh, fx.cls, k=1)
        # k=1 时单贡献者格不再是保护格（需要少于 1 家，即不存在）
        self.assertEqual(office.get_slice(sid1).protected, frozenset())
        sid3 = office.freeze_from(fx.wh, fx.cls, k=3)
        # k=3 时 D1FA（2 家）也变成保护格
        self.assertEqual(
            office.get_slice(sid3).protected,
            frozenset({("D1", "FA"), ("D2", "FA"), ("D2", "FB")}),
        )

    def test_scope_limits_enforced(self):
        # 5 个部门超过 4 行上限
        facts = [
            IssueFact(i, f"O{i}", "L1", "ISSUE", 1, "t") for i in range(1, 6)
        ]
        with self.assertRaises(ValidationError):
            build_slice(
                facts,
                {f"O{i}": f"D{i}" for i in range(1, 6)},
                {"L1": "F1"},
                (), k=2,
            )
        # 行合计超 200
        big = [IssueFact(1, "O1", "L1", "ISSUE", MAX_MARGIN + 1, "t")]
        with self.assertRaises(ValidationError):
            build_slice(big, {"O1": "D1"}, {"L1": "F1"}, (), k=2)

    def test_frozen_slice_immutable_to_new_events_and_reclassification(self):
        with tempfile.TemporaryDirectory() as tmp:
            wh_path = Path(tmp) / "wh.db"
            cls_path = Path(tmp) / "cls.db"
            sl_path = Path(tmp) / "slice.db"
            cat = QualityCatalog(":memory:")
            lots = [make_lot("LA1", 100, dict(VOCAB))]
            cat.replace_lots(lots)
            wh = Warehouse(wh_path, cat)
            wh.register_lots(lots)
            fx_force(wh, lots, "O1", {"LA1": 5})
            wh.issue("O1", "LA1", 5)
            cls = ClassificationStore(cls_path)
            cls.replace_classification({"O1": "D1"}, {"LA1": "FA"})
            office = SliceOffice(sl_path)
            sid = office.freeze_from(wh, cls, k=2)
            before = office.get_slice(sid)
            office.close(); wh.close(); cls.close()

            # 重开后新领退 + 分类换代
            wh2 = Warehouse(wh_path, QualityCatalog(":memory:"))
            cls2 = ClassificationStore(cls_path)
            cls2.replace_classification({"O1": "D_X"}, {"LA1": "F_Y"})
            office2 = SliceOffice(sl_path)
            after = office2.get_slice(sid)
            self.assertEqual(after.matrix, before.matrix)
            self.assertEqual(after.rows, ("D1",))       # 旧分类维表保持
            self.assertEqual(after.cols, ("FA",))
            self.assertEqual(after.class_rev, 1)
            # 新事实不进旧切面
            self.assertEqual(office2.get_slice(sid).grams("D1", "FA"), 5)
            office2.close(); cls2.close(); wh2.close()

    def test_duplicate_freeze_inputs_rejected(self):
        fx = build_fixture()
        fx.full_classification()
        office = SliceOffice(":memory:")
        office.freeze_from(fx.wh, fx.cls, k=2, structural_zeros=[("D1", "FB")])
        with self.assertRaises(ValidationError):
            office.freeze_from(fx.wh, fx.cls, k=2, structural_zeros=[("D1", "FB")])

    def test_empty_facts_produce_all_zero_unprotected_slice(self):
        built = build_slice((), {"O1": "D1"}, {"L1": "F1"}, (), k=2)
        self.assertEqual(built["matrix"][("D1", "F1")], 0)
        self.assertEqual(built["contributors"][("D1", "F1")], 0)
        self.assertEqual(built["protected"], frozenset())
        self.assertEqual(built["row_totals"], {"D1": 0})
        self.assertEqual(built["col_totals"], {"F1": 0})


def fx_force(wh: Warehouse, lots, order_id: str, usage: dict) -> None:
    from blend.model import PlanResult, PlannedLot
    from blend.planner import evaluate_usage, usage_cost

    order = make_order(order_id, sum(usage.values()), dict(VOCAB), max_lots=4)
    by = {l.lot_id: l for l in lots}
    prod = evaluate_usage(lots, usage, source="forced")
    planned = tuple(
        PlannedLot(lid, g, by[lid].step_g, by[lid].cost_per_g, by[lid].fixed_fee)
        for lid, g in sorted(usage.items())
    )
    res = PlanResult(
        order_id, order.total_g, planned, prod,
        usage_cost(usage, by), sum(1 for g in usage.values() if g), "forced",
    )
    wh.confirm_preview(wh.preview_external(order, res))


if __name__ == "__main__":
    unittest.main()
