"""规划器测试：枚举核对、稳健语义、固定费、步长、K、字典序、INFEASIBLE。"""

import random
import unittest
from fractions import Fraction

from blend.model import ComponentConstraint, InfeasibleError, Order
from blend.planner import Planner, brute_force_best, evaluate_usage
from tests.factories import make_lot, make_order

F = Fraction


class PlannerBasicTest(unittest.TestCase):
    def test_simple_feasible_and_cost(self):
        lots = [
            make_lot("A", 50, {"c1": ("1/4", "1/2")}, cost_per_g=2, fixed_fee=3),
            make_lot("B", 50, {"c1": ("0", "1/4")}, cost_per_g=1),
        ]
        order = make_order("O1", 10, {"c1": ("0", "1/2")}, max_lots=2)
        res = Planner(order, lots).solve()
        self.assertEqual(sum(res.usage_vector), 10)
        # B 更便宜且区间更优，应全用 B
        self.assertEqual(res.usage_vector, (0, 10))
        self.assertEqual(res.total_cost, 10)
        # 稳健核账
        self.assertTrue(res.quality.is_robust_feasible(order.constraints))

    def test_infeasible_when_cap_too_tight(self):
        lots = [
            make_lot("A", 50, {"c1": ("1/2", "3/4")}),
            make_lot("B", 50, {"c1": ("1/2", "3/4")}),
        ]
        # 所有 lot 的下界就是 1/2，订单上界 1/3 → 必然无解
        order = make_order("O2", 10, {"c1": ("0", "1/3")}, max_lots=2)
        with self.assertRaises(InfeasibleError):
            Planner(order, lots).solve()
        self.assertIsNone(brute_force_best(order, lots))

    def test_step_gcd_infeasible(self):
        lots = [make_lot("A", 50, {"c1": ("0", "1")}, step_g=3)]
        order = make_order("O3", 10, {"c1": ("0", "1")})
        with self.assertRaises(InfeasibleError):
            Planner(order, lots).solve()

    def test_step_combination_feasible(self):
        lots = [
            make_lot("A", 50, {"c1": ("0", "1")}, step_g=3),
            make_lot("B", 50, {"c1": ("0", "1")}, step_g=2),
        ]
        order = make_order("O4", 10, {"c1": ("0", "1")}, max_lots=2)
        res = Planner(order, lots).solve()
        u = dict(zip(res.lot_ids_sorted, res.usage_vector))
        self.assertEqual(u["A"] % 3, 0)
        self.assertEqual(u["B"] % 2, 0)
        self.assertEqual(sum(res.usage_vector), 10)

    def test_capacity_respected(self):
        lots = [
            make_lot("A", 4, {"c1": ("0", "1")}, step_g=1),
            make_lot("B", 4, {"c1": ("0", "1")}, step_g=1),
        ]
        order = make_order("O5", 10, {"c1": ("0", "1")}, max_lots=2)
        with self.assertRaises(InfeasibleError):
            Planner(order, lots).solve()

    def test_k_limits_active_lots(self):
        # 每 lot 库存仅 4，M=10 时 K=1/K=2 容量都不够，K=3 才可行
        lots = [make_lot(f"L{i}", 4, {"c1": ("0", "1")}) for i in range(3)]
        order1 = make_order("OK1", 10, {"c1": ("0", "1")}, max_lots=1)
        with self.assertRaises(InfeasibleError):
            Planner(order1, lots).solve()
        order2 = make_order("OK2", 10, {"c1": ("0", "1")}, max_lots=2)
        with self.assertRaises(InfeasibleError):
            Planner(order2, lots).solve()
        order3 = make_order("OK3", 10, {"c1": ("0", "1")}, max_lots=3)
        res = Planner(order3, lots).solve()
        self.assertEqual(res.active_lot_count, 3)


class RobustSemanticsTest(unittest.TestCase):
    def setUp(self):
        # A：宽区间便宜；B：窄区间贵。约束 [0.4,0.6]
        self.lots = [
            make_lot("A", 50, {"c1": ("0", "1")}, cost_per_g=1),
            make_lot("B", 50, {"c1": ("2/5", "3/5")}, cost_per_g=5),
        ]
        self.order = make_order("ROB", 10, {"c1": ("2/5", "3/5")}, max_lots=2)

    def test_midpoint_passes_but_robust_fails(self):
        # 全 A：可达 [0,1]，中点 0.5 落在订单内，但稳健语义不合格
        product = evaluate_usage(self.lots, {"A": 10})
        self.assertTrue(product.midpoint_feasible(self.order.constraints))
        self.assertFalse(product.is_robust_feasible(self.order.constraints))

    def test_planner_avoids_midpoint_only_plan(self):
        res = Planner(self.order, self.lots).solve()
        # 稳健可行的唯一选择是全 B（贵），规划器不得贪图 A 便宜
        self.assertEqual(res.usage_vector, (0, 10))
        self.assertTrue(res.quality.is_robust_feasible(self.order.constraints))
        self.assertEqual(res.total_cost, 50)

    def test_naive_ceiling_rounding_breaks_but_exact_search_finds_plan(self):
        """连续比例逐料向上取整会突破杂质上界，精确整数搜索仍有解。"""
        lots = [
            make_lot("L1", 4, {"c1": ("0", "1/2")}, cost_per_g=1),    # hi 0.5，库存 4
            make_lot("L2", 4, {"c1": ("0", "1/2")}, cost_per_g=1),    # hi 0.5，库存 4
            make_lot("L3", 4, {"c1": ("0", "4/5")}, cost_per_g=1),    # hi 0.8，库存 4
        ]
        # M=10、每 lot 仅 4 g，任何方案都必须三个 lot 同时启用；
        # 连续方案约 (1.2, 1.2, 7.6) 形态在库存下取整后最“顺手”的 (2,2,8)
        # 还会超 L3 库存，朴素逐料向上取整思路给出的高 L3 配比普遍越界。
        order = make_order("RND", 10, {"c1": ("0", "3/5")}, max_lots=3)  # 质量上界 6 g
        naive = {"L1": 2, "L2": 2, "L3": 6}  # 超 L3 库存；且 hi=1+1+4.8=6.8>6
        self.assertFalse(
            evaluate_usage(lots, naive).is_robust_feasible(order.constraints)
        )
        res = Planner(order, lots).solve()
        self.assertTrue(res.quality.is_robust_feasible(order.constraints))
        self.assertEqual(sum(res.usage_vector), 10)
        # (3,4,3): hi 质量和 = 3·.5+4·.5+3·.8 = 5.9 ≤ 6，且为字典序最小可行点
        self.assertEqual(res.usage_vector, (3, 4, 3))
        # 独立全枚举一致
        self.assertEqual(brute_force_best(order, lots).usage_vector, (3, 4, 3))


class CostAndTieBreakTest(unittest.TestCase):
    def test_fixed_fee_changes_plan(self):
        # A 单克便宜但固定费高；B 无固定费、单克贵
        lots_fee = [
            make_lot("A", 50, {"c1": ("0", "1")}, cost_per_g=1, fixed_fee=100),
            make_lot("B", 50, {"c1": ("0", "1")}, cost_per_g=3, fixed_fee=0),
        ]
        order = make_order("FEE", 10, {"c1": ("0", "1")}, max_lots=2)
        res = Planner(order, lots_fee).solve()
        self.assertEqual(res.usage_vector, (0, 10))  # 30 < 110

        lots_nofee = [
            make_lot("A", 50, {"c1": ("0", "1")}, cost_per_g=1, fixed_fee=0),
            make_lot("B", 50, {"c1": ("0", "1")}, cost_per_g=3, fixed_fee=0),
        ]
        res2 = Planner(order, lots_nofee).solve()
        self.assertEqual(res2.usage_vector, (10, 0))  # 无固定费时 A 划算

    def test_lexicographic_tie_break(self):
        # 两个完全同质 lot，多个等成本等 lot 数方案 -> 排序 lot_id 向量字典序最小
        lots = [
            make_lot("A", 50, {"c1": ("0", "1")}, cost_per_g=2),
            make_lot("B", 50, {"c1": ("0", "1")}, cost_per_g=2),
        ]
        order = make_order("LEX", 4, {"c1": ("0", "1")}, max_lots=2)
        res = Planner(order, lots).solve()
        # (0,4) 是字典序最小（同时 lot 数也最少）
        self.assertEqual(res.usage_vector, (0, 4))

    def test_second_objective_min_lot_count(self):
        # 成本相同的前提下，启用 lot 更少者胜
        lots = [
            make_lot("A", 50, {"c1": ("0", "1")}, cost_per_g=2, fixed_fee=0),
            make_lot("B", 50, {"c1": ("0", "1")}, cost_per_g=2, fixed_fee=0),
        ]
        order = make_order("CNT", 4, {"c1": ("0", "1")}, max_lots=2)
        res = Planner(order, lots).solve()
        self.assertEqual(res.active_lot_count, 1)

    def test_rational_endpoints_exact(self):
        # 分母 3、7 的端点，压线可行必须精确判定
        lots = [
            make_lot("A", 30, {"c1": ("1/7", "1/3")}),
            make_lot("B", 30, {"c1": ("1/7", "1/3")}),
        ]
        order = make_order("RAT", 7, {"c1": ("1/7", "1/3")}, max_lots=2)
        res = Planner(order, lots).solve()
        self.assertTrue(res.quality.is_robust_feasible(order.constraints))
        # 把上界收紧到 1/7：仅当所有分数恰好取 1/7 才合格，而区间允许更大值 -> 无解
        tight = make_order("RAT2", 7, {"c1": ("0", "1/7")}, max_lots=2)
        with self.assertRaises(InfeasibleError):
            Planner(tight, lots).solve()


class FuzzAgainstBruteForce(unittest.TestCase):
    """随机小实例：分支定界与独立全枚举在可行性与最优性上完全一致。"""

    def test_random_instances_match(self):
        rng = random.Random(20261118)
        checked = 0
        for trial in range(300):
            n = rng.randint(1, 4)
            comps = ["c1", "c2"]
            lots = []
            for i in range(n):
                iv = {}
                for c in comps:
                    a = rng.randint(0, 4)
                    b = rng.randint(a, 4)
                    iv[c] = (F(a, 4), F(b, 4))
                lots.append(
                    make_lot(
                        f"L{i}",
                        rng.randint(1, 10),
                        iv,
                        step_g=rng.randint(1, 3),
                        cost_per_g=rng.randint(0, 5),
                        fixed_fee=rng.choice([0, 0, 0, 7]),
                    )
                )
            M = rng.randint(1, 12)
            # 约束放宽：从 0 起步的宽上界 + 低下界，保证产生足够多可行实例，
            # 同时仍保留紧约束（约 1/4 概率）覆盖 INFEASIBLE 路径。
            bounds = {}
            for c in comps:
                if rng.random() < 0.75:
                    bounds[c] = (F(0), F(rng.randint(2, 4), 4))
                else:
                    a = rng.randint(0, 4)
                    b = rng.randint(a, 4)
                    bounds[c] = (F(a, 4), F(b, 4))
            order = Order(
                f"FUZZ{trial}", M,
                tuple(ComponentConstraint(c, *bounds[c]) for c in comps),
                max_lots=rng.randint(1, n),
            )
            exact = Planner(order, lots).solve_or_none()
            brute = brute_force_best(order, lots)
            if brute is None:
                self.assertIsNone(exact, f"trial {trial}: 枚举判无解而规划器给了方案")
                continue
            checked += 1
            self.assertIsNotNone(exact, f"trial {trial}: 枚举有解而规划器判无解")
            self.assertEqual(exact.total_cost, brute.total_cost, f"trial {trial} 成本不一致")
            self.assertEqual(exact.active_lot_count, brute.active_lot_count,
                             f"trial {trial} lot 数不一致")
            self.assertEqual(exact.usage_vector, brute.usage_vector,
                             f"trial {trial} 字典序最优不一致")
            self.assertTrue(exact.quality.is_robust_feasible(order.constraints))
        self.assertGreater(checked, 40)  # 确保确实核对了足够多可行实例


if __name__ == "__main__":
    unittest.main()
