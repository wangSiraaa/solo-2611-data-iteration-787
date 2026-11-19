"""履约仓测试：预览不占料、确认锁、并发争料、质量换代、领料/退回/取消。"""

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from blend.catalog import QualityCatalog
from blend.model import (
    ConflictError,
    InfeasibleError,
    StateError,
    StalePreviewError,
    ValidationError,
)
from blend.warehouse import Warehouse
from tests.factories import make_lot, make_order


class WarehouseFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "wh.db")
        self.cat_path = str(Path(self.tmp.name) / "cat.db")
        self.cat = QualityCatalog(self.cat_path)
        self.lots = [
            make_lot("L1", 20, {"c1": ("1/4", "1/2"), "c2": ("0", "1/10")},
                     step_g=2, cost_per_g=3, fixed_fee=5),
            make_lot("L2", 20, {"c1": ("0", "1/4"), "c2": ("1/4", "1/2")},
                     step_g=1, cost_per_g=2),
        ]
        self.cat.replace_lots(self.lots, note="r1")
        self.wh = Warehouse(self.db_path, self.cat)
        self.wh.register_lots(self.lots)

    def tearDown(self):
        self.wh.close()
        self.cat.close()
        self.tmp.cleanup()

    def _order(self):
        return make_order("ORD1", 8, {"c1": ("0", "3/4"), "c2": ("0", "1")}, max_lots=2)

    def test_preview_does_not_consume_stock(self):
        before = {r["lot_id"]: r["available_g"] for r in self.wh.inventory()}
        pv = self.wh.preview_order(self._order())
        after = {r["lot_id"]: r["available_g"] for r in self.wh.inventory()}
        self.assertEqual(before, after)
        self.assertEqual(self.wh.get_preview(pv)["created_rev"], 1)

    def test_confirm_consumes_and_ledger_written(self):
        pv = self.wh.preview_order(self._order())
        order_id = self.wh.confirm_preview(pv)
        self.assertEqual(order_id, "ORD1")
        view = self.wh.get_order(order_id)
        used = sum(view.plan["usage"].values())
        self.assertEqual(used, 8)
        # 库存口径：allocated 上升，available 下降
        inv = {r["lot_id"]: r for r in self.wh.inventory()}
        self.assertEqual(inv["L1"]["allocated_g"] + inv["L2"]["allocated_g"], 8)
        events = [e.event for e in view.ledger]
        self.assertIn("CONFIRM", events)
        # 重复确认拒绝
        with self.assertRaises(StateError):
            self.wh.confirm_preview(pv)

    def test_stale_preview_rejected_after_catalog_revision(self):
        pv = self.wh.preview_order(self._order())
        # 先确认一个订单占用部分库存，再验证 stale 拒绝不会污染既有占料
        self.wh.confirm_preview(pv)
        inv_after_first = {r["lot_id"]: r["allocated_g"] for r in self.wh.inventory()}

        # 换代前先出预览（绑定 rev1）
        pv_stale = self.wh.preview_order(
            make_order("ORD-STALE", 6, {"c1": ("0", "3/4"), "c2": ("0", "1")}, max_lots=2)
        )
        revised = [
            make_lot("L1", 20, {"c1": ("7/10", "9/10"), "c2": ("0", "1/10")},
                     step_g=2, cost_per_g=3, fixed_fee=5),
            make_lot("L2", 20, {"c1": ("1/2", "3/4"), "c2": ("1/4", "1/2")},
                     step_g=1, cost_per_g=2),
        ]
        self.cat.replace_lots(revised, note="r2 杂质换代")
        with self.assertRaises(StalePreviewError):
            self.wh.confirm_preview(pv_stale)
        # 被拒预览有状态标记，且库存与首个订单确认后完全一致（无幽灵占料）
        self.assertEqual(self.wh.get_preview(pv_stale)["status"], "REJECTED_STALE")
        inv_now = {r["lot_id"]: r["allocated_g"] for r in self.wh.inventory()}
        self.assertEqual(inv_now, inv_after_first)
        self.assertEqual(self.wh.list_orders(), ["ORD1"])
        # 被拒预览不能二次确认
        with self.assertRaises(StateError):
            self.wh.confirm_preview(pv_stale)
        # 重新规划后的新预览（新订单号）可以按 rev2 确认
        pv2 = self.wh.preview_order(
            make_order("ORD2", 8, {"c1": ("0", "1"), "c2": ("0", "1")}, max_lots=2)
        )
        self.wh.confirm_preview(pv2)
        self.assertEqual(self.wh.get_order("ORD2").confirmed_rev, 2)

    def test_interval_revision_changes_feasibility_and_optimum(self):
        """题面：真实改变杂质区间应影响可行性或最优用量。"""
        # rev1：L1 干净便宜、L2 杂质偏高，最优方案应全用 L1
        r1 = [
            make_lot("L1", 20, {"c1": ("0", "1/10")}, step_g=1, cost_per_g=1),
            make_lot("L2", 20, {"c1": ("1/2", "3/4")}, step_g=1, cost_per_g=1),
        ]
        cat = QualityCatalog(":memory:")
        cat.replace_lots(r1)
        wh = Warehouse(":memory:", cat)
        wh.register_lots(r1)
        order = make_order("SHIFT", 8, {"c1": ("0", "1/5")}, max_lots=2)
        pv1 = wh.preview_order(order)
        wh.confirm_preview(pv1)
        self.assertEqual(wh.get_order("SHIFT").plan["usage"], {"L1": 8})

        # rev2：L1 杂质区间上修到 1/2 → 在订单 [0,1/5] 下 L1 不再稳健可行；
        # 但 L2 下界就是 1/2，同样不满足 → 整个订单转 INFEASIBLE
        r2 = [
            make_lot("L1", 20, {"c1": ("1/4", "1/2")}, step_g=1, cost_per_g=1),
            make_lot("L2", 20, {"c1": ("1/2", "3/4")}, step_g=1, cost_per_g=1),
        ]
        cat.replace_lots(r2, note="杂质上修")
        with self.assertRaises(InfeasibleError):
            wh.plan_order(order)

        # 放宽订单后重新可行：此时只剩 L2 能选，最优用量从 L1 切到 L2
        wide = make_order("SHIFT2", 8, {"c1": ("1/2", "3/4")}, max_lots=2)
        pv2 = wh.preview_order(wide)
        wh.confirm_preview(pv2)
        self.assertEqual(wh.get_order("SHIFT2").plan["usage"], {"L2": 8})
        # 旧订单来源与边界不受目录换代影响
        self.assertEqual(wh.get_order("SHIFT").confirmed_rev, 1)
        wh.close(); cat.close()

    def test_issue_partial_full_and_over_issue_rejected(self):
        pv = self.wh.preview_order(self._order())
        self.wh.confirm_preview(pv)
        usage = self.wh.get_order("ORD1").plan["usage"]
        lot, grams = next(iter(usage.items()))
        self.wh.issue("ORD1", lot, grams)
        view = self.wh.get_order("ORD1")
        alloc = next(a for a in view.allocations if a.lot_id == lot)
        self.assertEqual(alloc.issued_g, grams)
        self.assertEqual(alloc.pending_g, 0)
        # 超领拒绝
        with self.assertRaises(ConflictError):
            self.wh.issue("ORD1", lot, 1)
        # 全部领完后订单进入 FULLY_ISSUED
        for lot2, g2 in usage.items():
            if lot2 != lot:
                self.wh.issue("ORD1", lot2, g2)
        self.assertEqual(self.wh.get_order("ORD1").status, "FULLY_ISSUED")

    def test_cancel_releases_only_unissued_and_return_flow(self):
        pv = self.wh.preview_order(self._order())
        self.wh.confirm_preview(pv)
        usage = self.wh.get_order("ORD1").plan["usage"]
        first_lot, first_g = next(iter(usage.items()))
        avail_before_issue = self.wh.available_g(first_lot)

        # 先领走一部分
        self.wh.issue("ORD1", first_lot, first_g)
        released = self.wh.cancel_order("ORD1")
        # 已领 lot 不释放；其余 lot 的待领份额释放
        self.assertNotIn(first_lot, released)
        view = self.wh.get_order("ORD1")
        self.assertEqual(view.status, "CANCELLED")
        for a in view.allocations:
            if a.lot_id == first_lot:
                self.assertEqual(a.pending_g, 0)
                self.assertEqual(a.issued_g, first_g)
            else:
                self.assertEqual(a.state, "RELEASED")
                self.assertEqual(a.pending_g, 0)
        # 已领部分没有复活：该 lot 可用量不包含已领克数
        self.assertEqual(self.wh.available_g(first_lot), avail_before_issue)
        # 取消后不能再领料
        with self.assertRaises(StateError):
            self.wh.issue("ORD1", first_lot, 1)
        # 已领配料只能退回；退回后库存回升且台账双留痕
        self.wh.return_material("ORD1", first_lot, first_g)
        self.assertEqual(self.wh.available_g(first_lot), avail_before_issue + first_g)
        events = [e.event for e in self.wh.get_order("ORD1").ledger]
        self.assertIn("ISSUE", events)
        self.assertIn("RETURN", events)
        # 不能退超过已领
        with self.assertRaises(ConflictError):
            self.wh.return_material("ORD1", first_lot, 1)
        # 重复取消拒绝
        with self.assertRaises(StateError):
            self.wh.cancel_order("ORD1")

    def test_returned_stock_is_reusable(self):
        order = make_order("ORD-A", 6, {"c1": ("0", "1"), "c2": ("0", "1")}, max_lots=2)
        pv = self.wh.preview_order(order)
        self.wh.confirm_preview(pv)
        usage = self.wh.get_order("ORD-A").plan["usage"]
        lot, grams = next(iter(usage.items()))
        self.wh.issue("ORD-A", lot, grams)
        self.wh.return_material("ORD-A", lot, grams)
        # 退回入库后，新订单可以再次花这份料
        pv2 = self.wh.preview_order(make_order("ORD-B", 6, {"c1": ("0", "1"), "c2": ("0", "1")}))
        # 不应抛库存冲突
        self.wh.confirm_preview(pv2)

    def test_external_product_accepted_without_catalog_binding(self):
        from blend.planner import Planner

        standalone_wh = Warehouse(":memory:", catalog=None)
        standalone_wh.register_lots(self.lots)
        order = self._order()
        result = Planner(order, self.lots).solve()
        pv = standalone_wh.preview_external(order, result)
        self.assertIsNone(standalone_wh.get_preview(pv)["created_rev"])
        standalone_wh.confirm_preview(pv)
        view = standalone_wh.get_order("ORD1")
        self.assertEqual(view.confirmed_rev, None)
        self.assertEqual(view.quality_src, result.quality.source)

    def test_persistence_across_reopen(self):
        pv = self.wh.preview_order(self._order())
        self.wh.confirm_preview(pv)
        usage = self.wh.get_order("ORD1").plan["usage"]
        lot, grams = next(iter(usage.items()))
        self.wh.issue("ORD1", lot, grams)
        self.wh.close()

        reopened = Warehouse(self.db_path, QualityCatalog(self.cat_path))
        view = reopened.get_order("ORD1")
        self.assertEqual(view.status, "PARTIAL_ISSUED" if len(usage) > 1 else "FULLY_ISSUED")
        self.assertEqual(sum(view.plan["usage"].values()), 8)
        inv = {r["lot_id"]: r for r in reopened.inventory()}
        self.assertEqual(inv[lot]["issued_g"], grams)
        reopened.close()


class ConcurrencyTest(unittest.TestCase):
    """两个独立连接争抢同一份最后库存：恰好一个成功，无双花。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "wh.db")
        self.cat_path = str(Path(self.tmp.name) / "cat.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_last_stock_race(self):
        cat = QualityCatalog(self.cat_path)
        lots = [make_lot("RARE", 10, {"c1": ("0", "1")}, step_g=1, cost_per_g=1)]
        cat.replace_lots(lots)
        wh = Warehouse(self.db_path, cat)
        wh.register_lots(lots)

        # 两个订单各要 10 g，而 RARE 总共只有 10 g
        order_a = make_order("RACE-A", 10, {"c1": ("0", "1")}, max_lots=1)
        order_b = make_order("RACE-B", 10, {"c1": ("0", "1")}, max_lots=1)
        pv_a = wh.preview_order(order_a)
        pv_b = wh.preview_order(order_b)
        wh.close()
        cat.close()

        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        errors: list[Exception] = []

        def worker(pv_id: int, tag: str):
            c = QualityCatalog(self.cat_path)
            w = Warehouse(self.db_path, c)
            try:
                barrier.wait()
                w.confirm_preview(pv_id)
                outcomes.append(f"{tag}:ok")
            except ConflictError as exc:
                outcomes.append(f"{tag}:conflict")
                errors.append(exc)
            except sqlite3.OperationalError as exc:  # 锁等待超时也算正确拦截
                outcomes.append(f"{tag}:lock")
                errors.append(exc)
            finally:
                w.close()
                c.close()

        t1 = threading.Thread(target=worker, args=(pv_a, "A"))
        t2 = threading.Thread(target=worker, args=(pv_b, "B"))
        t1.start(); t2.start()
        t1.join(); t2.join()

        self.assertEqual(len(outcomes), 2)
        ok = [o for o in outcomes if o.endswith(":ok")]
        blocked = [o for o in outcomes if not o.endswith(":ok")]
        self.assertEqual(len(ok), 1, outcomes)
        self.assertEqual(len(blocked), 1)
        # 事后核对：库存守恒，只有 10 g 被占用，没有双花
        audit = Warehouse(self.db_path, QualityCatalog(self.cat_path))
        inv = audit.inventory()[0]
        self.assertEqual(inv["allocated_g"], 10)
        self.assertEqual(inv["available_g"], 0)
        self.assertEqual(len(audit.list_orders()), 1)
        audit.close()


if __name__ == "__main__":
    unittest.main()
