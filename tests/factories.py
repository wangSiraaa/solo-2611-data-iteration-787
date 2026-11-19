"""测试公共夹具：合成 lot / 订单构造器。数据全为虚构。"""

from fractions import Fraction

from blend.model import ComponentConstraint, Lot, Order

F = Fraction


def make_lot(
    lot_id: str,
    available_g: int,
    intervals: dict,
    step_g: int = 1,
    cost_per_g: int = 1,
    fixed_fee: int = 0,
) -> Lot:
    iv = {
        comp: (F(lo), F(hi)) if not isinstance(lo, Fraction) else (lo, hi)
        for comp, (lo, hi) in intervals.items()
    }
    return Lot(lot_id, available_g, iv, step_g, cost_per_g, fixed_fee)


def make_order(
    order_id: str,
    total_g: int,
    bounds: dict,
    max_lots: int = 4,
    allowed=None,
) -> Order:
    cs = [
        ComponentConstraint(comp, F(lo), F(hi))
        for comp, (lo, hi) in sorted(bounds.items())
    ]
    return Order(order_id, total_g, cs, max_lots, frozenset(allowed) if allowed else None)


# 一个四成分的标准词表，供多测试共享
VOCAB = ("c1", "c2", "c3", "c4")
