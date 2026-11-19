"""整数混配规划器：精确分支定界，稳健质量语义。

输入是 :class:`~blend.model.Order` 与一批 :class:`~blend.model.Lot`（不依赖
目录对象，可独立调用；目录只是 lot 的另一种来源）。搜索空间::

    u_i ∈ {0} ∪ {step_i, 2·step_i, …, min(available_i, M)}   （整数克）
    Σ u_i = M
    #{i : u_i > 0} ≤ K
    对每个成分 c 与目录允许的所有分数取值：
        L_c ≤ Σ_i u_i·x_{i,c} / M ≤ H_c  恒成立

稳健语义按独立区间算术落地为整系数线性不等式：

* 上界不超标：``Σ u_i·hi_{i,c} ≤ H_c·M``
* 下界不缺位：``Σ u_i·lo_{i,c} ≥ L_c·M``

所有 Fraction 端点统一放大为整数，搜索全程整数运算，无浮点误差。
三级字典序目标：(1) 总成本最小；(2) 正用量 lot 数最少；(3) 按排序 lot_id
的用量向量字典序最小。**不做连续比例再舍入**，每个候选本身就是可行整数点。

规模上限（12 批、M≤300、步长 1..20、4 成分）下直接精确搜索；另提供
:func:`brute_force_best` 对小库存做全枚举，用于核对最优性。
"""

from __future__ import annotations

import itertools
from fractions import Fraction
from math import gcd
from typing import Mapping, Sequence

from .model import (
    ComponentConstraint,
    InfeasibleError,
    Lot,
    Order,
    PlannedLot,
    PlanResult,
    UsageProduct,
    ValidationError,
)

__all__ = [
    "Planner",
    "brute_force_best",
    "usage_cost",
    "active_count",
    "explain_plan",
]


# ----------------------------------------------------------------- 小工具


def usage_cost(usage: Mapping[str, int], lots: Mapping[str, Lot]) -> int:
    """方案总成本 = Σ 克数·单克成本 + 正用量 lot 的启用固定费。"""
    total = 0
    for lot_id, grams in usage.items():
        lot = lots[lot_id]
        total += grams * lot.cost_per_g
        if grams > 0:
            total += lot.fixed_fee
    return total


def active_count(usage: Mapping[str, int]) -> int:
    return sum(1 for g in usage.values() if g > 0)


class _Scaled:
    """把某一组成分约束放大到统一整数口径。"""

    def __init__(
        self,
        lots: Sequence[Lot],
        constraints: Sequence[ComponentConstraint],
        total_g: int,
    ) -> None:
        self.total_g = total_g
        self.components = tuple(c.component for c in constraints)
        cidx = {c: k for k, c in enumerate(self.components)}
        n, m = len(lots), len(self.components)

        # 每成分一个公共分母：所有 lot 端点与订单端点分母的 LCM
        denoms: list[int] = []
        for c in constraints:
            denoms.extend([c.lo.denominator, c.hi.denominator])
        for lot in lots:
            for comp in self.components:
                lo, hi = lot.intervals[comp]
                denoms.extend([lo.denominator, hi.denominator])
        scale = 1
        for d in denoms:
            scale = scale // gcd(scale, d) * d
        self.scale = scale

        # lot 端点整系数：lo_int = lo * scale
        self.lo = [[0] * m for _ in range(n)]
        self.hi = [[0] * m for _ in range(n)]
        for i, lot in enumerate(lots):
            for comp in self.components:
                lo, hi = lot.intervals[comp]
                k = cidx[comp]
                self.lo[i][k] = lo.numerator * (scale // lo.denominator)
                self.hi[i][k] = hi.numerator * (scale // hi.denominator)

        # 订单质量和界：L·M 与 H·M，同样放大 scale 倍
        self.L = [0] * m
        self.H = [0] * m
        for k, c in enumerate(constraints):
            self.L[k] = c.lo.numerator * (scale // c.lo.denominator) * total_g
            self.H[k] = c.hi.numerator * (scale // c.hi.denominator) * total_g


# ----------------------------------------------------------------- 规划器


class Planner:
    """精确整数混配规划器。

    Parameters
    ----------
    order:
        订单硬约束（M、成分闭区间、K、lot 白名单）。
    lots:
        可选原料批列表；每个 lot 的步长、可用量、成本随对象携带。
    """

    def __init__(self, order: Order, lots: Sequence[Lot]) -> None:
        self.order = order
        self.all_lots = sorted(lots, key=lambda x: x.lot_id)
        if not self.all_lots:
            raise ValidationError("规划器至少需要一个 lot")
        ids = [l.lot_id for l in self.all_lots]
        if len(ids) != len(set(ids)):
            raise ValidationError("lot_id 重复")
        comp_vocab = frozenset(self.all_lots[0].intervals)
        for lot in self.all_lots:
            if frozenset(lot.intervals) != comp_vocab:
                raise ValidationError("所有 lot 必须共享同一组成分词表")
        for c in order.constraints:
            if c.component not in comp_vocab:
                raise ValidationError(f"订单约束了目录中不存在的成分: {c.component}")

        if order.allowed_lot_ids is not None:
            allow = order.allowed_lot_ids
            self.lots = [l for l in self.all_lots if l.lot_id in allow]
            missing = allow - {l.lot_id for l in self.lots}
            if missing:
                raise ValidationError(f"白名单含未知 lot: {sorted(missing)}")
        else:
            self.lots = self.all_lots
        if not self.lots:
            raise ValidationError("白名单过滤后无可选 lot")

        self.n = len(self.lots)
        # K 超过批数没有意义，收紧不改变可行域（不改写传入的冻结订单）
        self.K = min(order.max_lots, self.n)

        self.scaled = _Scaled(self.lots, order.constraints, order.total_g)
        self.stats = {"nodes": 0, "subsets": 0, "subsets_skipped": 0, "pruned": 0}
        self._best: dict | None = None

    # ------------------------------------------------------------ 主入口
    def solve(self) -> PlanResult:
        """求最优方案；无可行整数方案时抛 :class:`InfeasibleError`。"""
        result = self._search()
        if result is None:
            raise InfeasibleError(
                f"订单 {self.order.order_id}: 不存在满足步长/库存/K/稳健质量的整数方案"
            )
        return result

    def solve_or_none(self) -> PlanResult | None:
        return self._search()

    # ------------------------------------------------------------ 搜索骨架
    def _domains(self, idx_list: Sequence[int]) -> list[list[int]]:
        """每个 lot 的可行克数域：{0} ∪ 步长倍数（截断到库存与 M）。"""
        M = self.order.total_g
        domains: list[list[int]] = []
        for i in idx_list:
            lot = self.lots[i]
            cap = min(lot.available_g, M)
            dom = list(range(0, cap + 1, lot.step_g))  # 含 0
            domains.append(dom)
        return domains

    def _search(self) -> PlanResult | None:
        M = self.order.total_g
        K = self.K
        lot_ids = [l.lot_id for l in self.lots]
        steps = [l.step_g for l in self.lots]
        caps = [min(l.available_g, M) for l in self.lots]

        # 枚举活跃 lot 子集（大小 1..K），子集内 DFS 分配克数。
        for size in range(1, K + 1):
            for subset in itertools.combinations(range(self.n), size):
                self.stats["subsets"] += 1
                if self._subset_trivially_infeasible(subset, caps, steps, M):
                    self.stats["subsets_skipped"] += 1
                    continue
                self._dfs_subset(subset, caps, M)

        if self._best is None:
            return None
        usage = dict(zip(lot_ids, self._best["u"]))
        return self._build_result(usage)

    def _subset_trivially_infeasible(
        self,
        subset: tuple[int, ...],
        caps: list[int],
        steps: list[int],
        M: int,
    ) -> bool:
        # 容量不足
        if sum(caps[i] for i in subset) < M:
            return True
        # 步长 gcd 不整除 M（必要条件）
        g = 0
        for i in subset:
            g = gcd(g, steps[i])
        if M % g != 0:
            return True
        # 质量包络：固定总克数 M 下，成分 c 的质量和可达范围 ⊆
        # [M·min lo, M·max hi]（按 lot 取端点极值），与订单区间不相交则跳过
        sc = self.scaled
        for k in range(len(sc.components)):
            env_lo = M * min(sc.lo[i][k] for i in subset)
            env_hi = M * max(sc.hi[i][k] for i in subset)
            if env_hi < sc.L[k] or env_lo > sc.H[k]:
                return True
        return False

    def _dfs_subset(self, subset: tuple[int, ...], caps: list[int], M: int) -> None:
        """在指定活跃 lot 子集内做定长 DFS。"""
        sc = self.scaled
        m = len(sc.components)
        lots = self.lots
        u = [0] * self.n
        # 累积的整系数质量和 A_c = Σ(已分配 u_i)·端点系数
        A_lo = [0] * m
        A_hi = [0] * m
        # 成本（线性部分随分配累积，固定费在取正用量时计入）
        lin_cost = 0
        fix_cost = 0
        active = 0
        pos = 0
        nsub = len(subset)

        # 后缀信息：用于容量/gcd/质量/成本剪枝
        suffix_cap = [0] * (nsub + 1)
        suffix_gcd = [0] * (nsub + 1)
        suffix_min_lo = [[0] * m for _ in range(nsub + 1)]
        suffix_max_hi = [[0] * m for _ in range(nsub + 1)]
        suffix_min_cpg = [0] * (nsub + 1)
        for p in range(nsub - 1, -1, -1):
            i = subset[p]
            suffix_cap[p] = suffix_cap[p + 1] + caps[i]
            suffix_gcd[p] = gcd(suffix_gcd[p + 1], lots[i].step_g)
            suffix_min_cpg[p] = (
                lots[i].cost_per_g
                if p == nsub - 1
                else min(lots[i].cost_per_g, suffix_min_cpg[p + 1])
            )
            for k in range(m):
                suffix_min_lo[p][k] = (
                    sc.lo[i][k]
                    if p == nsub - 1
                    else min(sc.lo[i][k], suffix_min_lo[p + 1][k])
                )
                suffix_max_hi[p][k] = (
                    sc.hi[i][k]
                    if p == nsub - 1
                    else max(sc.hi[i][k], suffix_max_hi[p + 1][k])
                )

        def dfs(pos: int, rem: int) -> None:
            nonlocal lin_cost, fix_cost, active
            self.stats["nodes"] += 1

            # ---- 剪枝 1：剩余克数的容量与步长可达性 ----------------
            if rem > suffix_cap[pos]:
                self.stats["pruned"] += 1
                return
            if rem > 0 and rem % suffix_gcd[pos] != 0:
                self.stats["pruned"] += 1
                return

            # ---- 剪枝 2：稳健质量的可达包络（精确整数） -------------
            # 剩余 rem 克在后缀 lot 间分配，成分 c 的额外贡献落在
            # [rem·min lo, rem·max hi] 内；与已累积 A 相加后必须与订单
            # 质量区间 [L,H] 有交集，否则任何完整方案都不可能稳健合格。
            for k in range(m):
                reach_lo = A_lo[k] + rem * suffix_min_lo[pos][k]
                reach_hi = A_hi[k] + rem * suffix_max_hi[pos][k]
                if reach_hi < sc.L[k] or reach_lo > sc.H[k]:
                    self.stats["pruned"] += 1
                    return

            # ---- 剪枝 3：成本下界（线性部分精确下界 + 已锁定固定费）
            lb = lin_cost + fix_cost
            if rem:
                lb += rem * suffix_min_cpg[pos]
            if self._best is not None and lb > self._best["cost"]:
                self.stats["pruned"] += 1
                return

            # ---- 叶子：恰好凑满 M ----------------------------------
            if pos == nsub:
                if rem != 0:
                    return
                # 完整方案的稳健质量判定（整系数，双侧）
                for k in range(m):
                    if A_hi[k] > sc.H[k] or A_lo[k] < sc.L[k]:
                        return
                if active > self.K:
                    return
                self._consider(u, lin_cost + fix_cost, active)
                return

            i = subset[pos]
            lot = lots[i]
            cap = min(caps[i], rem)
            # 给后续 lot 留足容量：u_i >= rem - suffix_cap[pos+1]
            min_u = max(0, rem - suffix_cap[pos + 1])
            # 升序尝试，天然偏向字典序小的向量
            for grams in range(0, cap + 1, lot.step_g):
                if grams < min_u:
                    continue
                u[i] = grams
                if grams:
                    active += 1
                    fix_cost += lot.fixed_fee
                    lin_cost += grams * lot.cost_per_g
                    for k in range(m):
                        A_lo[k] += grams * sc.lo[i][k]
                        A_hi[k] += grams * sc.hi[i][k]
                dfs(pos + 1, rem - grams)
                if grams:
                    for k in range(m):
                        A_lo[k] -= grams * sc.lo[i][k]
                        A_hi[k] -= grams * sc.hi[i][k]
                    lin_cost -= grams * lot.cost_per_g
                    fix_cost -= lot.fixed_fee
                    active -= 1
                u[i] = 0

        dfs(0, M)

    # ------------------------------------------------------------ 最优性比较
    def _consider(self, u: list[int], cost: int, active: int) -> None:
        """按 (成本, lot 数, 排序 lot_id 用量向量字典序) 三级比较保最优。"""
        candidate = {"u": tuple(u), "cost": cost, "active": active}
        if self._best is None:
            self._best = candidate
            return
        b = self._best
        key_c = (candidate["cost"], candidate["active"], candidate["u"])
        key_b = (b["cost"], b["active"], b["u"])
        if key_c < key_b:
            self._best = candidate

    # ------------------------------------------------------------ 结果装配
    def _build_result(self, usage: Mapping[str, int]) -> PlanResult:
        lots = {l.lot_id: l for l in self.lots}
        planned = tuple(
            PlannedLot(
                lot_id=l.lot_id,
                grams=usage.get(l.lot_id, 0),
                step_g=l.step_g,
                cost_per_g=l.cost_per_g,
                fixed_fee=l.fixed_fee,
            )
            for l in self.lots  # 已按 lot_id 排序
        )
        product = evaluate_usage(self.lots, usage, source="planner")
        return PlanResult(
            order_id=self.order.order_id,
            total_g=self.order.total_g,
            lots=planned,
            quality=product,
            total_cost=usage_cost(usage, lots),
            active_lot_count=active_count(usage),
            source=product.source,
        )


# ------------------------------------------------------------ 独立质量评估


def evaluate_usage(
    lots: Sequence[Lot],
    usage: Mapping[str, int],
    source: str = "external",
) -> UsageProduct:
    """不依赖目录，直接对 lot 列表 + 用量组合计算质量边界产品。

    仓库借此可以消费来自目录或外部的任意 :class:`UsageProduct`，
    规划器自身也用它装配方案产品。
    """
    by_id = {l.lot_id: l for l in lots}
    clean: dict[str, int] = {}
    for lot_id, grams in usage.items():
        if lot_id not in by_id:
            raise ValidationError(f"用量中出现未知 lot: {lot_id!r}")
        if not isinstance(grams, int) or grams < 0:
            raise ValidationError(f"lot {lot_id} 用量必须是非负整数")
        if grams:
            clean[lot_id] = grams
    total = sum(clean.values())
    if total <= 0:
        raise ValidationError("用量合计必须为正整数")

    comps = tuple(sorted(lots[0].intervals))
    mass_bounds: dict[str, tuple[Fraction, Fraction]] = {}
    frac_bounds: dict[str, tuple[Fraction, Fraction]] = {}
    for comp in comps:
        m_lo = Fraction(0)
        m_hi = Fraction(0)
        for lot_id, grams in clean.items():
            lo, hi = by_id[lot_id].intervals[comp]
            m_lo += grams * lo
            m_hi += grams * hi
        mass_bounds[comp] = (m_lo, m_hi)
        frac_bounds[comp] = (m_lo / total, m_hi / total)
    return UsageProduct(
        usage=clean,
        total_g=total,
        mass_bounds=mass_bounds,
        fraction_bounds=frac_bounds,
        source=source,
        components=frozenset(comps),
    )


# ------------------------------------------------------------ 全枚举核对器


def brute_force_best(order: Order, lots: Sequence[Lot]) -> PlanResult | None:
    """小库存全枚举核对：展开每个 lot 的克数域做笛卡尔积穷举。

    与 :class:`Planner` 完全独立的第二条实现，用于测试中逐实例核对
    最优性与 INFEASIBLE 判定。规模随批数/域大小指数增长，仅适合小例。
    """
    lots = sorted(lots, key=lambda x: x.lot_id)
    if order.allowed_lot_ids is not None:
        lots = [l for l in lots if l.lot_id in order.allowed_lot_ids]
    M = order.total_g
    domains: list[list[int]] = []
    for lot in lots:
        cap = min(lot.available_g, M)
        domains.append(list(range(0, cap + 1, lot.step_g)))

    best: tuple | None = None
    best_usage: dict[str, int] | None = None
    for combo in itertools.product(*domains):
        if sum(combo) != M:
            continue
        if sum(1 for g in combo if g > 0) > order.max_lots:
            continue
        usage = {lot.lot_id: g for lot, g in zip(lots, combo)}
        product = evaluate_usage(lots, usage, source="brute-force")
        if not product.is_robust_feasible(order.constraints):
            continue
        cost = usage_cost(usage, {l.lot_id: l for l in lots})
        key = (cost, sum(1 for g in combo if g > 0), combo)
        if best is None or key < best:
            best = key
            best_usage = usage

    if best_usage is None:
        return None
    planned = tuple(
        PlannedLot(
            lot_id=l.lot_id,
            grams=best_usage.get(l.lot_id, 0),
            step_g=l.step_g,
            cost_per_g=l.cost_per_g,
            fixed_fee=l.fixed_fee,
        )
        for l in lots
    )
    product = evaluate_usage(lots, best_usage, source="brute-force")
    return PlanResult(
        order_id=order.order_id,
        total_g=M,
        lots=planned,
        quality=product,
        total_cost=best[0],
        active_lot_count=best[1],
        source="brute-force",
    )


# ------------------------------------------------------------ 解释入口


def explain_plan(result: PlanResult, order: Order) -> str:
    """把方案讲清楚：用量、成本构成、稳健质量边界对订单区间的包含关系。"""
    lines: list[str] = []
    lines.append(f"订单 {result.order_id} 方案（来源 {result.source}）")
    lines.append(f"  总克数 {result.total_g}，启用 lot {result.active_lot_count} 个，"
                 f"总成本 {result.total_cost}")
    lines.append("  用量明细：")
    for pl in result.lots:
        if pl.grams == 0:
            continue
        lines.append(
            f"    - {pl.lot_id}: {pl.grams} g（步长 {pl.step_g}），"
            f"线性 {pl.grams}×{pl.cost_per_g}={pl.grams * pl.cost_per_g}"
            f" + 固定费 {pl.fixed_fee} = {pl.line_cost}"
        )
    lines.append("  稳健质量核账（整段可达分数区间须落入订单闭区间）：")
    for c in order.constraints:
        lo, hi = result.quality.fraction_bounds[c.component]
        ok = lo >= c.lo and hi <= c.hi
        lines.append(
            f"    - {c.component}: 可达 [{_f(lo)}, {_f(hi)}] vs 订单 "
            f"[{_f(c.lo)}, {_f(c.hi)}] -> {'合格' if ok else '不合格'}"
        )
    lines.append("  排序 lot_id 用量向量: " + str(result.usage_vector))
    return "\n".join(lines)


def _f(x: Fraction) -> str:
    if x.denominator == 1:
        return str(x.numerator)
    return f"{x.numerator}/{x.denominator}"
