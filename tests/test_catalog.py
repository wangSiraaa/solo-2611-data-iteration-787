"""目录边界计算、修订原子性与重开持久化测试。"""

import sqlite3
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from blend.catalog import QualityCatalog
from blend.model import Lot, ValidationError
from tests.factories import make_lot

F = Fraction


class CatalogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cat = QualityCatalog(":memory:")

    def tearDown(self) -> None:
        self.cat.close()

    def _base_lots(self):
        return [
            make_lot("L1", 100, {"c1": ("1/10", "1/5"), "c2": ("0", "1/2")},
                     cost_per_g=2, fixed_fee=5),
            make_lot("L2", 80, {"c1": ("1/4", "1/3"), "c2": ("1/4", "1/4")},
                     cost_per_g=1),
        ]

    def test_revision_and_basic_boundary(self):
        rev = self.cat.replace_lots(self._base_lots(), note="init")
        self.assertEqual(rev, 1)
        self.assertEqual(self.cat.revision, 1)
        product = self.cat.product({"L1": 30, "L2": 20})
        self.assertEqual(product.total_g, 50)
        # c1: 30*[1/10,1/5] + 20*[1/4,1/3]
        self.assertEqual(product.mass_bounds["c1"],
                         (30 * F(1, 10) + 20 * F(1, 4),
                          30 * F(1, 5) + 20 * F(1, 3)))
        # 分数界 = 质量界 / 50
        lo, hi = product.fraction_bounds["c1"]
        self.assertEqual(lo, product.mass_bounds["c1"][0] / 50)
        self.assertEqual(hi, product.mass_bounds["c1"][1] / 50)
        # c2 下界来自 L1 的 0 + L2 的 1/4
        self.assertEqual(product.mass_bounds["c2"][0], 20 * F(1, 4))
        self.assertEqual(product.source, "catalog@rev1")

    def test_zero_usage_lots_dropped_but_allowed(self):
        self.cat.replace_lots(self._base_lots())
        product = self.cat.product({"L1": 40, "L2": 0})
        self.assertEqual(set(product.usage), {"L1"})
        self.assertEqual(product.total_g, 40)

    def test_unknown_lot_and_bad_usage_rejected(self):
        self.cat.replace_lots(self._base_lots())
        with self.assertRaises(ValidationError):
            self.cat.product({"NOPE": 10})
        with self.assertRaises(ValidationError):
            self.cat.product({"L1": -3})
        with self.assertRaises(ValidationError):
            self.cat.product({"L1": 0, "L2": 0})

    def test_revision_atomic_failure_leaves_state_unchanged(self):
        self.cat.replace_lots(self._base_lots(), note="init")
        good = self.cat.get_lots()

        # lot_id 重复 → 目录层拒绝并整体回滚，修订号不动
        dup = list(good) + [make_lot("L1", 5, {"c1": ("0", "1"), "c2": ("0", "1")})]
        with self.assertRaises(ValidationError):
            self.cat.replace_lots(dup)
        self.assertEqual(self.cat.revision, 1)
        self.assertEqual([l.lot_id for l in self.cat.get_lots()], ["L1", "L2"])

        # 跨修订允许更换词表（旧修订仍永久可查），这里只验证新修订生效
        new_vocab = [make_lot("X1", 5, {"cx": ("0", "1")})]
        self.cat.replace_lots(new_vocab, note="换词表")
        self.assertEqual(self.cat.revision, 2)
        self.assertEqual(self.cat.components(), ("cx",))
        # 旧修订词表与数据仍可精确读出
        self.assertEqual(self.cat.components(rev=1), ("c1", "c2"))

        # 模拟事务内 SQL 落库失败：直接在事务里插坏行，确认回滚后状态不变
        rev_before = self.cat.revision
        with self.assertRaises(sqlite3.Error):
            self.cat._conn.execute("BEGIN")
            self.cat._conn.execute(
                "INSERT INTO cat_lot_rev(rev, lot_id, seq, available_g, step_g,"
                " cost_per_g, fixed_fee) VALUES (99, 'GHOST', 0, -1, 1, 0, 0)"
            )
            # 触发 NOT NULL 约束失败
            self.cat._conn.execute(
                "INSERT INTO cat_revision(rev, created_at, note) VALUES (99, NULL, NULL)"
            )
        self.cat._conn.rollback()
        self.assertEqual(self.cat.revision, rev_before)
        self.assertEqual(
            self.cat._conn.execute(
                "SELECT COUNT(*) FROM cat_lot_rev WHERE lot_id='GHOST'"
            ).fetchone()[0],
            0,
        )

    def test_successful_revision_keeps_history_and_old_rev_readable(self):
        self.cat.replace_lots(self._base_lots(), note="r1")
        new_lots = [
            make_lot("L1", 50, {"c1": ("1/2", "3/5"), "c2": ("0", "1/10")}),
            make_lot("L2", 200, {"c1": ("0", "1/10"), "c2": ("1/2", "1/2")}),
        ]
        rev2 = self.cat.replace_lots(new_lots, note="r2")
        self.assertEqual(rev2, 2)
        revs = self.cat.revisions()
        self.assertEqual([r[0] for r in revs], [1, 2])
        # 旧修订仍可精确读出
        old = {l.lot_id: l for l in self.cat.get_lots(rev=1)}
        self.assertEqual(old["L1"].available_g, 100)
        self.assertEqual(old["L1"].intervals["c1"], (F(1, 10), F(1, 5)))
        cur = {l.lot_id: l for l in self.cat.get_lots()}
        self.assertEqual(cur["L1"].available_g, 50)
        # 旧修订号上的产品仍可计算
        p_old = self.cat.product({"L1": 10}, rev=1)
        self.assertEqual(p_old.mass_bounds["c1"], (F(1, 1), F(2, 1)))
        with self.assertRaises(ValidationError):
            self.cat.get_lots(rev=99)

    def test_persistence_across_reopen(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cat.db"
            cat = QualityCatalog(path)
            cat.replace_lots(self._base_lots(), note="init")
            cat.close()

            reopened = QualityCatalog(path)
            self.assertEqual(reopened.revision, 1)
            lots = reopened.get_lots()
            self.assertEqual(lots[0].intervals["c1"], (F(1, 10), F(1, 5)))
            product = reopened.product({"L1": 10, "L2": 10})
            self.assertEqual(product.total_g, 20)
            reopened.close()

    def test_lot_validation(self):
        with self.assertRaises(ValidationError):
            Lot("x", -1, {"c1": (F(0), F(1))})
        with self.assertRaises(ValidationError):
            Lot("x", 10, {"c1": (F(2), F(1))})
        with self.assertRaises(ValidationError):
            Lot("x", 10, {"c1": (F(1, 2), F(1, 3))})
        with self.assertRaises(ValidationError):
            Lot("x", 10, {"c1": (F(0), F(1))}, step_g=0)
        with self.assertRaises(ValidationError):
            Lot("x", 10, {"c1": (F(0), F(1))}, cost_per_g=-1)


if __name__ == "__main__":
    unittest.main()
