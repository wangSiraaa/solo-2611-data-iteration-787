"""端到端演示：题面要求的全部关键场景，数据均为合成。

运行：``python3 demo.py``
场景：
  1. 目录独立质量产品与精确边界
  2. 中点可行而稳健不行（规划器拒绝便宜但区间宽的原料）
  3. 固定费改变最优方案
  4. 包装步长约束
  5. 预览不占料 → 确认事务 → 领料 → 取消只放未领 → 退回留痕
  6. 并发争料：两个独立连接抢最后库存，恰好一个成功
  7. 质量换代：旧预览被拒、重新规划、旧确认保持来源
  8. 最大规模（12 批 / M=300 / 4 成分）性能与全枚举核对
"""

from __future__ import annotations

import threading
from fractions import Fraction

from blend.catalog import QualityCatalog
from blend.model import (
    ComponentConstraint,
    ConflictError,
    Order,
    StalePreviewError,
)
from blend.planner import Planner, brute_force_best, evaluate_usage, explain_plan
from blend.warehouse import Warehouse
from tests.factories import make_lot, make_order

F = Fraction
BANNER = "=" * 72


def section(title: str) -> None:
    print("\n" + BANNER)
    print(title)
    print(BANNER)


# ---------------------------------------------------------------- 场景 1


def demo_catalog_product() -> None:
    section("场景 1｜目录独立计算质量产品（不感知订单）")
    cat = QualityCatalog(":memory:")
    lots = [
        make_lot("L1", 100, {"c1": ("1/10", "1/5"), "c2": ("0", "1/2")}),
        make_lot("L2", 80, {"c1": ("1/4", "1/3"), "c2": ("1/4", "1/4")}),
    ]
    rev = cat.replace_lots(lots, note="合成目录 rev1")
    product = cat.product({"L1": 30, "L2": 20})
    print(f"修订号 rev={rev}，用量 30g L1 + 20g L2，总量 {product.total_g}g")
    for comp in product.components:
        mlo, mhi = product.mass_bounds[comp]
        flo, fhi = product.fraction_bounds[comp]
        print(f"  {comp}: 质量和 ∈ [{mlo}, {mhi}] g，分数 ∈ [{flo}, {fhi}]")
    cat.close()


# ---------------------------------------------------------------- 场景 2


def demo_midpoint_vs_robust() -> None:
    section("场景 2｜中点可行而稳健不行")
    lots = [
        make_lot("CHEAP", 50, {"c1": ("0", "1")}, cost_per_g=1),       # 宽区间便宜
        make_lot("STABLE", 50, {"c1": ("2/5", "3/5")}, cost_per_g=5),  # 窄区间贵
    ]
    order = make_order("MID", 10, {"c1": ("2/5", "3/5")}, max_lots=2)

    only_cheap = evaluate_usage(lots, {"CHEAP": 10})
    print("全用便宜料 CHEAP 的可达分数区间:", only_cheap.fraction_bounds["c1"])
    print("  中点判定:", only_cheap.midpoint_feasible(order.constraints),
          "｜稳健判定:", only_cheap.is_robust_feasible(order.constraints))

    result = Planner(order, lots).solve()
    print(explain_plan(result, order))
    assert result.usage_vector == (0, 10), "稳健语义下必须选 STABLE"


# ---------------------------------------------------------------- 场景 3


def demo_fixed_fee() -> None:
    section("场景 3｜固定费改变方案")
    order = make_order("FEE", 10, {"c1": ("0", "1")}, max_lots=2)
    with_fee = [
        make_lot("A", 50, {"c1": ("0", "1")}, cost_per_g=1, fixed_fee=100),
        make_lot("B", 50, {"c1": ("0", "1")}, cost_per_g=3, fixed_fee=0),
    ]
    r1 = Planner(order, with_fee).solve()
    print("A 带 100 固定费:", r1.usage_vector, "成本", r1.total_cost)

    no_fee = [
        make_lot("A", 50, {"c1": ("0", "1")}, cost_per_g=1, fixed_fee=0),
        make_lot("B", 50, {"c1": ("0", "1")}, cost_per_g=3, fixed_fee=0),
    ]
    r2 = Planner(order, no_fee).solve()
    print("A 无固定费    :", r2.usage_vector, "成本", r2.total_cost)
    assert r1.usage_vector != r2.usage_vector


# ---------------------------------------------------------------- 场景 4


def demo_steps() -> None:
    section("场景 4｜包装步长（不得连续比例随意舍入）")
    lots = [
        make_lot("P3", 50, {"c1": ("0", "1")}, step_g=3, cost_per_g=2),
        make_lot("P2", 50, {"c1": ("0", "1")}, step_g=2, cost_per_g=1),
    ]
    order = make_order("STP", 10, {"c1": ("0", "1")}, max_lots=2)
    result = Planner(order, lots).solve()
    u = dict(zip(result.lot_ids_sorted, result.usage_vector))
    print("方案:", u, "（P3 用量是 3 的倍数，P2 用量是 2 的倍数，合计 10）")
    assert u["P3"] % 3 == 0 and u["P2"] % 2 == 0 and sum(u.values()) == 10

    # M 不被步长 gcd 整除时明确 INFEASIBLE
    odd_order = make_order("STP-X", 7, {"c1": ("0", "1")}, max_lots=1)
    try:
        Planner(odd_order, [lots[0]]).solve()
    except Exception as exc:
        print("单 lot 步长 3 凑 7g ->", type(exc).__name__)


# ---------------------------------------------------------------- 场景 5


def demo_warehouse_lifecycle() -> None:
    section("场景 5｜预览不占料 → 确认 → 领料 → 取消 → 退回 全流程")
    cat = QualityCatalog(":memory:")
    lots = [
        make_lot("L1", 20, {"c1": ("1/4", "1/2"), "c2": ("0", "1/10")},
                 step_g=2, cost_per_g=3, fixed_fee=5),
        make_lot("L2", 20, {"c1": ("0", "1/4"), "c2": ("1/4", "1/2")},
                 step_g=1, cost_per_g=2),
    ]
    cat.replace_lots(lots)
    wh = Warehouse(":memory:", cat)
    wh.register_lots(lots)

    order = make_order("W1", 8, {"c1": ("0", "3/4"), "c2": ("0", "1")}, max_lots=2)
    pv = wh.preview_order(order)
    print("预览 pv_id =", pv, "后库存（应未变）:",
          {r["lot_id"]: r["available_g"] for r in wh.inventory()})

    wh.confirm_preview(pv)
    print("确认后库存:")
    for r in wh.inventory():
        print(" ", r)

    view = wh.get_order("W1")
    first_lot, first_g = next(iter(view.plan["usage"].items()))
    issue_g = min(3, first_g)  # 只领一部分，留下待领份额供取消释放
    wh.issue("W1", first_lot, issue_g)
    print(f"实际领料 {first_lot} {issue_g}g（仍有 {first_g - issue_g}g 待领）")

    released = wh.cancel_order("W1")
    print("取消释放（仅未领份额）:", released)
    wh.return_material("W1", first_lot, issue_g)
    print(f"已领的 {issue_g}g 登记退回，库存回升至 {wh.available_g(first_lot)}g")
    print(wh.explain_order("W1"))
    wh.close(); cat.close()


# ---------------------------------------------------------------- 场景 6


def demo_concurrent_race() -> None:
    section("场景 6｜并发争料：两个独立连接抢同一份最后库存")
    import tempfile
    from pathlib import Path

    tmp = tempfile.mkdtemp()
    db_path = str(Path(tmp) / "wh.db")
    cat_path = str(Path(tmp) / "cat.db")

    cat = QualityCatalog(cat_path)
    lots = [make_lot("RARE", 10, {"c1": ("0", "1")}, step_g=1)]
    cat.replace_lots(lots)
    wh = Warehouse(db_path, cat)
    wh.register_lots(lots)
    pv_a = wh.preview_order(make_order("RACE-A", 10, {"c1": ("0", "1")}, max_lots=1))
    pv_b = wh.preview_order(make_order("RACE-B", 10, {"c1": ("0", "1")}, max_lots=1))
    wh.close(); cat.close()

    barrier = threading.Barrier(2)
    results: list[str] = []

    def worker(pv_id: int, tag: str) -> None:
        c = QualityCatalog(cat_path)
        w = Warehouse(db_path, c)
        try:
            barrier.wait()
            w.confirm_preview(pv_id)
            results.append(f"{tag} 确认成功")
        except ConflictError:
            results.append(f"{tag} 被事务库存核对拦截（ConflictError）")
        finally:
            w.close(); c.close()

    t1 = threading.Thread(target=worker, args=(pv_a, "连接A"))
    t2 = threading.Thread(target=worker, args=(pv_b, "连接B"))
    t1.start(); t2.start(); t1.join(); t2.join()
    for r in sorted(results):
        print(" ", r)

    audit = Warehouse(db_path, QualityCatalog(cat_path))
    inv = audit.inventory()[0]
    print(f"事后审计 RARE: 占用 {inv['allocated_g']}g，可用 {inv['available_g']}g，"
          f"订单数 {len(audit.list_orders())}（无双花）")
    audit.close()


# ---------------------------------------------------------------- 场景 7


def demo_quality_generation() -> None:
    section("场景 7｜质量换代：旧预览拒绝、重新规划、旧确认保持来源")
    cat = QualityCatalog(":memory:")
    lots_v1 = [
        make_lot("L1", 20, {"c1": ("1/4", "1/2")}, step_g=2, cost_per_g=3, fixed_fee=5),
        make_lot("L2", 20, {"c1": ("0", "1/4")}, step_g=1, cost_per_g=2),
    ]
    cat.replace_lots(lots_v1, note="rev1")
    wh = Warehouse(":memory:", cat)
    wh.register_lots(lots_v1)

    order = make_order("GEN", 8, {"c1": ("0", "3/4")}, max_lots=2)
    pv_old = wh.preview_order(order)
    wh.confirm_preview(pv_old)
    old_view = wh.get_order("GEN")
    print("rev1 下确认订单 GEN，来源标记:", old_view.quality_src,
          "确认修订:", old_view.confirmed_rev)

    # 换代前先为 GEN2 生成预览（绑定 rev1），随后目录才换代
    pv_stale = wh.preview_order(
        make_order("GEN2", 8, {"c1": ("0", "3/4")}, max_lots=2)
    )
    # 目录换代：杂质区间整体变化
    lots_v2 = [
        make_lot("L1", 20, {"c1": ("7/10", "9/10")}, step_g=2, cost_per_g=3, fixed_fee=5),
        make_lot("L2", 20, {"c1": ("1/2", "3/4")}, step_g=1, cost_per_g=2),
    ]
    cat.replace_lots(lots_v2, note="rev2 杂质换代")
    try:
        wh.confirm_preview(pv_stale)
    except StalePreviewError as exc:
        print("换代后确认旧预览被拒:", exc)
    assert wh.get_preview(pv_stale)["status"] == "REJECTED_STALE"
    assert all(r["allocated_g"] == (8 if r["lot_id"] == "L2" else 0)
               for r in wh.inventory()), "stale 回滚后库存必须与 GEN 确认后一致"

    # 放宽约束后按新质量重新规划；旧订单的来源边界原样保留
    pv_new = wh.preview_order(
        make_order("GEN3", 8, {"c1": ("0", "1")}, max_lots=2)
    )
    wh.confirm_preview(pv_new)
    print("新预览按 rev2 确认，订单 GEN3 确认修订:",
          wh.get_order("GEN3").confirmed_rev)
    print("旧订单 GEN 仍保持来源:", wh.get_order("GEN").quality_src,
          "修订", wh.get_order("GEN").confirmed_rev)
    wh.close(); cat.close()


# ---------------------------------------------------------------- 场景 8


def demo_max_scale_and_brute_force() -> None:
    section("场景 8｜最大规模性能 + 小库存独立枚举核对最优性")
    import random
    import time

    rng = random.Random(7)
    comps = ["c1", "c2", "c3", "c4"]
    # 带重试的确定性合成：放宽订单上界直到实例可行，保证演示命中有解路径
    result = None
    for attempt in range(200):
        lots = []
        for i in range(12):
            iv = {}
            for c in comps:
                a = rng.randint(0, 4)
                iv[c] = (F(a, 10), F(rng.randint(a * 2, 8), 10))  # hi <= 0.8
            lots.append(
                make_lot(
                    f"L{i:02d}", rng.randint(30, 90), iv,
                    step_g=rng.randint(1, 12),
                    cost_per_g=rng.randint(1, 9),
                    fixed_fee=rng.choice([0, 0, 0, 50]),
                )
            )
        constraints = [
            ComponentConstraint("c1", F(0), F(9, 10)),
            ComponentConstraint("c2", F(0), F(9, 10)),
            ComponentConstraint("c3", F(0), F(4, 5)),
            ComponentConstraint("c4", F(0), F(9, 10)),
        ]
        order = Order("BIG", 300, constraints, max_lots=6)
        planner = Planner(order, lots)
        result = planner.solve_or_none()
        if result is not None:
            break
    assert result is not None, "合成器重试 200 次仍无可行实例"
    t0 = time.perf_counter()
    result = planner.solve()  # 复跑取稳定计时（搜索本身确定性）
    dt = time.perf_counter() - t0
    print(f"12 批 / M=300 / 4 成分：耗时 {dt*1000:.1f} ms，"
          f"搜索节点 {planner.stats['nodes']}，剪枝 {planner.stats['pruned']} 次，"
          f"子集跳过 {planner.stats['subsets_skipped']}/{planner.stats['subsets']}")
    print("最优用量:", {l.lot_id: l.grams for l in result.lots if l.grams})
    print("总成本:", result.total_cost, "｜活跃 lot:", result.active_lot_count)
    assert result.quality.is_robust_feasible(constraints)

    # 小库存：分支定界 vs 独立全枚举
    small = [
        make_lot("S1", 6, {"c1": ("0", "1/2"), "c2": ("1/4", "1/2")},
                 step_g=2, cost_per_g=3, fixed_fee=4),
        make_lot("S2", 6, {"c1": ("1/4", "3/4"), "c2": ("0", "1/3")},
                 step_g=1, cost_per_g=2),
        make_lot("S3", 6, {"c1": ("0", "2/5"), "c2": ("1/5", "3/5")},
                 step_g=3, cost_per_g=5, fixed_fee=2),
    ]
    small_order = Order(
        "SMALL", 9,
        (ComponentConstraint("c1", F(0), F(3, 5)),
         ComponentConstraint("c2", F(0), F(1, 2))),
        max_lots=3,
    )
    r_bb = Planner(small_order, small).solve()
    r_bf = brute_force_best(small_order, small)
    print("\n小库存核对：分支定界", r_bb.usage_vector, "成本", r_bb.total_cost)
    print("          独立全枚举", r_bf.usage_vector, "成本", r_bf.total_cost)
    assert r_bb.usage_vector == r_bf.usage_vector
    assert r_bb.total_cost == r_bf.total_cost
    print("两者完全一致，最优性得到独立枚举确认。")


def main() -> None:
    demo_catalog_product()
    demo_midpoint_vs_robust()
    demo_fixed_fee()
    demo_steps()
    demo_warehouse_lifecycle()
    demo_concurrent_race()
    demo_quality_generation()
    demo_max_scale_and_brute_force()
    print("\n全部演示场景完成。")


if __name__ == "__main__":
    main()
