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
  9. 稀疏领料交叉表：部分退/全退计数 → 冻结切面 → 定向披露 → 拒绝后历史不变 → 关库重开
"""

from __future__ import annotations

import threading
from fractions import Fraction

from blend.catalog import QualityCatalog
from blend.classification import ClassificationStore
from blend.disclosure import DisclosureOffice
from blend.model import (
    ClassificationGapError,
    ComponentConstraint,
    ConflictError,
    DisclosureRejected,
    Order,
    PlanResult,
    PlannedLot,
    StalePreviewError,
    StructuralZeroViolation,
)
from blend.planner import Planner, brute_force_best, evaluate_usage, explain_plan, usage_cost
from blend.slice import SliceOffice
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


# ---------------------------------------------------------------- 场景 9


def _force_usage(wh: Warehouse, lots, order_id: str, usage: dict) -> None:
    """用外部质量产品入口把指定整数用量落进履约仓（演示造数用）。"""
    order = make_order(order_id, sum(usage.values()), {"c1": ("0", "1")}, max_lots=4)
    by = {l.lot_id: l for l in lots}
    product = evaluate_usage(lots, usage, source="demo-forced")
    planned = tuple(
        PlannedLot(lid, g, by[lid].step_g, by[lid].cost_per_g, by[lid].fixed_fee)
        for lid, g in sorted(usage.items())
    )
    result = PlanResult(
        order_id, order.total_g, planned, product,
        usage_cost(usage, by), sum(1 for g in usage.values() if g), "demo-forced",
    )
    wh.confirm_preview(wh.preview_external(order, result))


def demo_sparse_crosstab_disclosure() -> None:
    section("场景 9｜稀疏领料交叉表：冻结切面 + 定向披露 + 历史合并 + 关库重开")
    import tempfile
    from pathlib import Path

    tmp = tempfile.mkdtemp()
    cat = QualityCatalog(":memory:")
    # 3 个物料族（F1/F2/F3），每族两个 lot
    lots = [
        make_lot("L11", 100, {"c1": ("0", "1")}, cost_per_g=1),
        make_lot("L12", 100, {"c1": ("0", "1")}, cost_per_g=1),
        make_lot("L21", 100, {"c1": ("0", "1")}, cost_per_g=1),
        make_lot("L22", 100, {"c1": ("0", "1")}, cost_per_g=1),
        make_lot("L31", 100, {"c1": ("0", "1")}, cost_per_g=1),
        make_lot("L32", 100, {"c1": ("0", "1")}, cost_per_g=1),
    ]
    cat.replace_lots(lots)
    wh_path = Path(tmp) / "wh.db"
    cls_path = Path(tmp) / "cls.db"
    sl_path = Path(tmp) / "slice.db"
    wh = Warehouse(wh_path, cat)
    wh.register_lots(lots)

    # ---- 真实领退：一次部分退料 + 一次全额退料 + 一次取消释放 ----------
    # D1 行（多订单，故各格贡献者>=2 不保护）：
    #   O1 在 F1 分两次领 4；O2 在 F1 领 3（F1 共两家、净 7）
    _force_usage(wh, lots, "O1", {"L11": 4})
    wh.issue("O1", "L11", 1); wh.issue("O1", "L11", 3)   # 同订单分两次领
    _force_usage(wh, lots, "O2", {"L12": 3})
    wh.issue("O2", "L12", 3)
    #   O3 在 F2 领 7 后部分退回 2 -> 净 5；O4 在 F2 领 5
    _force_usage(wh, lots, "O3", {"L21": 7})
    wh.issue("O3", "L21", 7)
    wh.return_material("O3", "L21", 2)                   # 部分退料
    _force_usage(wh, lots, "O4", {"L22": 5})
    wh.issue("O4", "L22", 5)
    #   O5 在 F3 领 3；O6 在 F3 领 8 后全额退回 -> 净 0，退出贡献者；
    #   O5b 在 F3 领 4，使该格由 O5、O5b 两家贡献（非保护），O6 全退不计
    _force_usage(wh, lots, "O5", {"L31": 3})
    wh.issue("O5", "L31", 3)
    _force_usage(wh, lots, "O5B", {"L32": 4})
    wh.issue("O5B", "L32", 4)
    _force_usage(wh, lots, "O6", {"L31": 8})
    wh.issue("O6", "L31", 8)
    wh.return_material("O6", "L31", 8)                   # 全额退料
    # D2/D3 行各格只有一家贡献者 => 主保护格
    _force_usage(wh, lots, "O7", {"L11": 6, "L21": 2, "L31": 4})
    wh.issue("O7", "L11", 6); wh.issue("O7", "L21", 2); wh.issue("O7", "L31", 4)
    _force_usage(wh, lots, "O8", {"L12": 3, "L22": 7, "L32": 2})
    wh.issue("O8", "L12", 3); wh.issue("O8", "L22", 7); wh.issue("O8", "L32", 2)
    # O9 占料后取消，从未领出 => 不计入切面
    _force_usage(wh, lots, "O9", {"L11": 9})
    wh.cancel_order("O9")
    print("领退事实（仅 ISSUE/RETURN）：")
    for f in wh.issue_facts():
        print(f"  #{f.ledger_id} {f.event:6} {f.order_id}/{f.lot_id} {f.signed_g:+d} g")

    # ---- 分类：先演示缺口报错，再补全 -----------------------------------
    cls = ClassificationStore(cls_path)
    cls.replace_classification(
        {"O1": "D1", "O2": "D1", "O3": "D1"},
        {"L11": "F1", "L12": "F1", "L21": "F2"},
    )
    office = SliceOffice(sl_path)
    try:
        office.freeze_from(wh, cls, k=2)
    except ClassificationGapError as exc:
        print("分类不全被拦截，待分派订单：", exc.missing_orders,
              "待分派 lot：", exc.missing_lots)
    full_orders = {f"O{i}": ("D1" if i <= 6 else "D2" if i == 7 else "D3")
                   for i in range(1, 9)}
    full_orders["O5B"] = "D1"
    full_orders["O9"] = "D3"
    full_lots = {"L11": "F1", "L12": "F1", "L21": "F2", "L22": "F2",
                 "L31": "F3", "L32": "F3"}
    cls.replace_classification(full_orders, full_lots, note="全量分类 rev2")
    # 结构性零冲突演示：声明 D3×F1 不可能，但 O8 在 L12(F1) 净领 3
    try:
        office.freeze_from(wh, cls, k=2, structural_zeros=[("D3", "F1")])
    except StructuralZeroViolation as exc:
        print("结构性零冲突被拦截：", exc.violating_cells)

    # ---- 冻结切面（k=2，声明 D1×F3 之外无结构零；这里用一个真实空组合）----
    # D1×F3 因 O6 全退而为普通零（不声明成结构性零）；声明 D2 无 F? 之外的
    # 真实结构零：本例用 D3 与 F? 无冲突的清单，故传空，展示普通零 != 结构零。
    sid = office.freeze_from(wh, cls, k=2, structural_zeros=(), note="季度切面")
    sl = office.get_slice(sid)
    print(f"\n冻结切面 slice_id={sid}，边界台账号 {sl.boundary_id}，"
          f"分类 rev{sl.class_rev}，门槛 k={sl.k}")
    print("内网精确矩阵（克数/贡献者数）：")
    print("        " + "".join(f"{c:>10}" for c in sl.cols) + "    行合计")
    for r in sl.rows:
        cells = "".join(f"{sl.matrix[(r, c)]:>6}/{sl.contributors[(r, c)]:>2}"
                        for c in sl.cols)
        print(f"  {r:>5}{cells}    {sl.row_totals[r]}")
    print("  列合计  " + "".join(f"{sl.col_totals[c]:>10}" for c in sl.cols))
    print("主保护格（非空且贡献者<k）：", sorted(sl.protected))
    print("D1×F3 有两家贡献者（O5、O5B），非保护格；O6 全退未计入：",
          sl.matrix[("D1", "F3")] == 7 and sl.contributors[("D1", "F3")] == 2
          and ("D1", "F3") not in sl.protected)
    print("对外固定公开（无格内克数/人数）：", office.public_view(sid))

    # ---- 定向披露 -------------------------------------------------------
    dl = DisclosureOffice(office)
    print("\n定向发布：")
    # 必选非保护格 D1×F2=10（O3 净5 + O4 的5，两家贡献者）
    dry = dl.check_request(sid, "甲", required=[("D1", "F2")])
    print("  甲请求 D1×F2 干跑结论：", "安全" if dry["accepted"] else dry["reason"])
    out = dl.request_disclosure(sid, "甲", required=[("D1", "F2")])
    print("  甲获准：", {f"{r}×{c}": g for (r, c), g in out.granted_values.items()})

    # 追加另一个非保护格 D1×F3（两家贡献者），在 3x3 自由度下仍安全
    out2 = dl.request_disclosure(sid, "甲", required=[("D1", "F3")])
    print("  甲追加 D1×F3 获准：",
          {f"{r}×{c}": g for (r, c), g in out2.granted_values.items()
           if (r, c) == ("D1", "F3")})

    # 点名主保护格 -> 拒绝且历史不变
    protected_sample = next(iter(sl.protected))
    try:
        dl.request_disclosure(sid, "甲", optional=[(protected_sample, 9)])
    except DisclosureRejected as exc:
        print(f"  甲点名主保护格 {protected_sample} 被拒：", exc.reason)
    print("  拒绝后甲的历史不变：", dl.granted_cells(sid, "甲"))

    # 已公开格重复请求不计价值
    r = dl.check_request(sid, "甲", optional=[(("D1", "F2"), 5)])
    print("  已公开格重复请求：新增价值 =", r["optional_value"], "，跳过", r["skipped"])

    # 新客户乙：独立历史，同一切面从零开始
    out_b = dl.request_disclosure(sid, "乙", required=[("D1", "F2")])
    print("  乙独立获准：", {f"{r}×{c}": g for (r, c), g in out_b.granted_values.items()})
    print("  请求审计日志：",
          [(x["customer"], x["accepted"], x["reason"]) for x in dl.request_log(sid)])
    print("  甲对外视图（只含合计与获准值，无人数/被盖原值）：")
    print("   ", dl.customer_view(sid, "甲"))

    # ---- 关库重开：冻结矩阵与披露历史都保持，新领退不改旧切面 -----------
    wh.close(); cls.close(); office.close(); dl.close(); cat.close()
    wh2 = Warehouse(wh_path, QualityCatalog(":memory:"))
    cls2 = ClassificationStore(cls_path)
    office2 = SliceOffice(sl_path)
    dl2 = DisclosureOffice(office2)
    sl_reopen = office2.get_slice(sid)
    print("\n关库重开后：")
    print("  冻结矩阵保持：", dict(sl_reopen.matrix))
    print("  甲历史保持：", dl2.granted_cells(sid, "甲"))
    print("  乙历史保持：", dl2.granted_cells(sid, "乙"))
    before = dict(sl_reopen.matrix)
    _force_usage(wh2, lots, "O10", {"L32": 11})
    wh2.issue("O10", "L32", 11)
    full_orders["O10"] = "D2"
    cls2.replace_classification(full_orders, full_lots)
    assert dict(office2.get_slice(sid).matrix) == before, "旧切面必须不变"
    sid_new = office2.freeze_from(wh2, cls2, k=2, structural_zeros=())
    print("  新领退后旧切面不变；新切面", sid_new, "矩阵：",
          dict(office2.get_slice(sid_new).matrix))
    wh2.close(); cls2.close(); office2.close(); dl2.close()


def main() -> None:
    demo_catalog_product()
    demo_midpoint_vs_robust()
    demo_fixed_fee()
    demo_steps()
    demo_warehouse_lifecycle()
    demo_concurrent_race()
    demo_quality_generation()
    demo_max_scale_and_brute_force()
    demo_sparse_crosstab_disclosure()
    print("\n全部演示场景完成。")


if __name__ == "__main__":
    main()
