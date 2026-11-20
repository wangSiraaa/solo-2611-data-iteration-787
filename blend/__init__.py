"""受限混配方案与实际领料质量核账。

六个独立可调用模块：

* :mod:`blend.model`          —— 不可变数据模型、精确有理数与统一异常；
* :mod:`blend.catalog`        —— SQLite 原料质量目录（lot 可用量 + 成分分数区间，原子修订）；
* :mod:`blend.planner`        —— 整数混配规划器（精确分支定界，稳健质量语义）；
* :mod:`blend.warehouse`      —— 履约仓（预览不占料、确认事务锁、领料/退回/取消）；
* :mod:`blend.classification` —— 版本化业务分类（订单→部门、lot→物料族，原子修订）；
* :mod:`blend.slice`          —— 冻结统计切面（净领克数矩阵、贡献者计数、结构性零）；
* :mod:`blend.disclosure`     —— 面向收件方的单元格定向披露（精确整数推断 + 历史合并）。

全部数据为合成数据，不对应任何真实食品或药品标准。
"""

from .model import (
    BlendError,
    ValidationError,
    InfeasibleError,
    StalePreviewError,
    ConflictError,
    StateError,
    ClassificationGapError,
    StructuralZeroViolation,
    DisclosureRejected,
    Lot,
    ComponentConstraint,
    Order,
    UsageProduct,
    PlanResult,
    PlannedLot,
    as_frac,
)

__all__ = [
    "BlendError",
    "ValidationError",
    "InfeasibleError",
    "StalePreviewError",
    "ConflictError",
    "StateError",
    "ClassificationGapError",
    "StructuralZeroViolation",
    "DisclosureRejected",
    "Lot",
    "ComponentConstraint",
    "Order",
    "UsageProduct",
    "PlanResult",
    "PlannedLot",
    "as_frac",
]
