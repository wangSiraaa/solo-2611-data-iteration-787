"""不可变数据模型、精确有理数与统一异常体系。

区间端点一律用 :class:`fractions.Fraction` 精确表示，规划器内部再统一放大成
整数做整数搜索，全链路不引入浮点误差。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Mapping, Sequence

__all__ = [
    "BlendError",
    "ValidationError",
    "InfeasibleError",
    "StalePreviewError",
    "ConflictError",
    "StateError",
    "Lot",
    "ComponentConstraint",
    "Order",
    "UsageProduct",
    "PlannedLot",
    "PlanResult",
    "as_frac",
]


# --------------------------------------------------------------------------- 异常


class BlendError(Exception):
    """本包所有自定义异常的基类。"""


class ValidationError(BlendError):
    """输入数据不满足题面约束（负数、端点倒挂、步长非法等）。"""


class InfeasibleError(BlendError):
    """不存在满足全部硬约束（含稳健质量）的整数方案。"""


class StalePreviewError(BlendError):
    """预览绑定的质量目录版本已被修订，确认被拒绝。"""


class ConflictError(BlendError):
    """确认时真实事务内库存核对失败（被别的连接抢先占用等）。"""


class StateError(BlendError):
    """操作与单据当前状态不符（重复确认、取消后再领料等）。"""


# --------------------------------------------------------------- 有理数工具


def as_frac(x: "int | Fraction | str | float") -> Fraction:
    """把端点/分数统一转成精确 :class:`Fraction`。

    接受 int、Fraction、十进制字符串（如 ``"0.25"``）以及形如 ``"3/7"``
    的有理字符串；拒绝 float 以避免静默精度损失。
    """
    if isinstance(x, Fraction):
        return x
    if isinstance(x, int):
        return Fraction(x)
    if isinstance(x, str):
        s = x.strip()
        if "/" in s:
            return Fraction(s)  # Fraction 本身支持 "3/7"
        return Fraction(s)
    raise ValidationError(f"不支持的有理数表示: {x!r}（请用 int/Fraction/str，勿用 float）")


def _check_fraction_01(v: Fraction, what: str) -> None:
    if v < 0 or v > 1:
        raise ValidationError(f"{what} 必须落在 [0,1]，当前 {v}")


# ----------------------------------------------------------------- 原料批


@dataclass(frozen=True)
class Lot:
    """一个原料批：稳定 lot_id、非负整数可用克数与各成分分数区间。

    ``intervals`` 为 ``成分名 -> (lo, hi)``，端点满足
    ``0 <= lo <= hi <= 1``。区间语义：同批原料每克的同一未知成分分数可在
    区间内任取，成分之间不声明任何相关性（独立区间语义）。
    """

    lot_id: str
    available_g: int
    intervals: Mapping[str, tuple[Fraction, Fraction]]
    step_g: int = 1
    cost_per_g: int = 0
    fixed_fee: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.lot_id, str) or not self.lot_id:
            raise ValidationError("lot_id 必须是非空字符串")
        if not isinstance(self.available_g, int) or self.available_g < 0:
            raise ValidationError(f"lot {self.lot_id}: 可用克数必须是非负整数")
        if not isinstance(self.step_g, int) or not (1 <= self.step_g <= 20):
            raise ValidationError(f"lot {self.lot_id}: 包装步长必须是 1..20 的整数")
        if not isinstance(self.cost_per_g, int) or self.cost_per_g < 0:
            raise ValidationError(f"lot {self.lot_id}: 单克成本必须是非负整数")
        if not isinstance(self.fixed_fee, int) or self.fixed_fee < 0:
            raise ValidationError(f"lot {self.lot_id}: 启用固定费必须是非负整数")
        iv = dict(self.intervals)
        for name, pair in iv.items():
            if not isinstance(name, str) or not name:
                raise ValidationError("成分名必须是非空字符串")
            try:
                lo, hi = pair
            except (TypeError, ValueError) as exc:
                raise ValidationError(f"lot {self.lot_id} 成分 {name}: 区间必须是 (lo, hi)") from exc
            lo, hi = as_frac(lo), as_frac(hi)
            _check_fraction_01(lo, f"lot {self.lot_id} 成分 {name} 的 lo")
            _check_fraction_01(hi, f"lot {self.lot_id} 成分 {name} 的 hi")
            if lo > hi:
                raise ValidationError(f"lot {self.lot_id} 成分 {name}: lo={lo} > hi={hi}")
            iv[name] = (lo, hi)
        object.__setattr__(self, "intervals", iv)


# --------------------------------------------------------------- 成分约束


@dataclass(frozen=True)
class ComponentConstraint:
    """订单对单个成分的闭分数区间 [lo, hi]，端点为精确有理数。"""

    component: str
    lo: Fraction
    hi: Fraction

    def __post_init__(self) -> None:
        if not isinstance(self.component, str) or not self.component:
            raise ValidationError("成分名必须是非空字符串")
        lo, hi = as_frac(self.lo), as_frac(self.hi)
        _check_fraction_01(lo, f"成分 {self.component} 的 lo")
        _check_fraction_01(hi, f"成分 {self.component} 的 hi")
        if lo > hi:
            raise ValidationError(f"成分 {self.component}: lo={lo} > hi={hi}")
        object.__setattr__(self, "lo", lo)
        object.__setattr__(self, "hi", hi)


# --------------------------------------------------------------------- 订单


@dataclass(frozen=True)
class Order:
    """一次混配订单的全部硬约束。

    * ``total_g``        正整数目标总克数 M；
    * ``constraints``    每个允许成分的闭分数区间；
    * ``max_lots``       正用量 lot 数上限 K；
    * ``allowed_lot_ids`` 可使用的 lot 白名单（None 表示由规划器入参全量）；
    * 步长、成本等随 :class:`Lot` 携带。
    """

    order_id: str
    total_g: int
    constraints: Sequence[ComponentConstraint]
    max_lots: int
    allowed_lot_ids: "frozenset[str] | None" = None

    def __post_init__(self) -> None:
        if not isinstance(self.order_id, str) or not self.order_id:
            raise ValidationError("order_id 必须是非空字符串")
        if not isinstance(self.total_g, int) or self.total_g <= 0:
            raise ValidationError(f"订单 {self.order_id}: 总克数 M 必须是正整数")
        if not isinstance(self.max_lots, int) or self.max_lots <= 0:
            raise ValidationError(f"订单 {self.order_id}: 种类上限 K 必须是正整数")
        cs = list(self.constraints)
        names = [c.component for c in cs]
        if len(names) != len(set(names)):
            raise ValidationError(f"订单 {self.order_id}: 成分约束存在重复成分")
        if not cs:
            raise ValidationError(f"订单 {self.order_id}: 至少需要一个成分约束")
        object.__setattr__(self, "constraints", tuple(cs))
        if self.allowed_lot_ids is not None:
            object.__setattr__(self, "allowed_lot_ids", frozenset(self.allowed_lot_ids))


# --------------------------------------------------------------- 质量产品


@dataclass(frozen=True)
class UsageProduct:
    """给定用量组合的质量边界产品（目录与规划器共同的通用语言）。

    * ``usage``          lot_id -> 非负整数克数；
    * ``total_g``        用量合计（规划场景恒为 M）；
    * ``mass_bounds``    成分 -> (质量和下界, 质量和上界)，单位克，Fraction；
    * ``fraction_bounds`` 成分 -> (分数下界, 分数上界) = 质量界 / total_g；
    * ``source``         产品来源标注（目录版本号 / 外部产品名等）。

    区间语义为独立区间算术：质量和上界取各 lot 上界之和、下界取各 lot
    下界之和；不对成分间相关性作任何假设。
    """

    usage: Mapping[str, int]
    total_g: int
    mass_bounds: Mapping[str, tuple[Fraction, Fraction]]
    fraction_bounds: Mapping[str, tuple[Fraction, Fraction]]
    source: str = "external"
    components: "frozenset[str]" = field(default_factory=frozenset)

    def is_robust_feasible(
        self, constraints: Sequence[ComponentConstraint]
    ) -> bool:
        """稳健合格判定：目录允许的**所有**成分取值都落在订单闭区间内。

        等价于每个成分的整个可达分数区间被订单区间包含。
        """
        for c in constraints:
            lo, hi = self.fraction_bounds[c.component]
            if lo < c.lo or hi > c.hi:
                return False
        return True

    def midpoint_feasible(
        self, constraints: Sequence[ComponentConstraint]
    ) -> bool:
        """仅用各成分可达区间中点判定（演示对照用，非履约语义）。"""
        for c in constraints:
            lo, hi = self.fraction_bounds[c.component]
            mid = (lo + hi) / 2
            if mid < c.lo or mid > c.hi:
                return False
        return True


# --------------------------------------------------------------- 规划结果


@dataclass(frozen=True)
class PlannedLot:
    """方案中单个 lot 的用量与成本明细。"""

    lot_id: str
    grams: int
    step_g: int
    cost_per_g: int
    fixed_fee: int

    @property
    def line_cost(self) -> int:
        return self.grams * self.cost_per_g + (self.fixed_fee if self.grams > 0 else 0)


@dataclass(frozen=True)
class PlanResult:
    """规划器交付的完整方案（INFEASIBLE 时不产出本对象，改抛异常）。"""

    order_id: str
    total_g: int
    lots: tuple[PlannedLot, ...]
    quality: UsageProduct
    total_cost: int
    active_lot_count: int
    source: str

    @property
    def usage_vector(self) -> tuple[int, ...]:
        """按排序 lot_id 的用量向量（字典序比较口径，含零用量位）。"""
        return tuple(l.grams for l in self.lots)

    @property
    def lot_ids_sorted(self) -> tuple[str, ...]:
        return tuple(l.lot_id for l in self.lots)
