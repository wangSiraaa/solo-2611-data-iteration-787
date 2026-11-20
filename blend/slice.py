"""冻结统计切面：「使用部门 × 物料族」净领克数矩阵的保密发布基座。

切面只消费履约仓的真实 ISSUE/RETURN 事实（:class:`~blend.warehouse.IssueFact`）
与版本化分类（:class:`~blend.classification.ClassificationStore`），自身不复制
业务状态机：

1. 每张订单在每个 (部门, 物料族) 格内，对切面边界之前的 ISSUE 取正、RETURN
   取负求净领克数；未领的确认（CONFIRM）与取消释放（CANCEL_RELEASE）不在事实
   视图内，天然不计；全部退回后净量为 0，该订单退出该格贡献者计数；
2. 每格累计非负净量，贡献者数 = 净量为正的**不同订单数**（同一订单分两次
   领仍只算一家）；
3. 切面固定源事件边界（台账号）、分类修订号与当时的支持门槛 ``k``；
4. 非空且正贡献者少于 ``k`` 的格为主保护格；
5. 结构性不可能组合由显式业务清单给出，一旦有正净量落在其中，切面直接失败
   （:class:`~blend.model.StructuralZeroViolation`）；普通零格不等于结构性零；
6. 分类覆盖不全时报 :class:`~blend.model.ClassificationGapError`，严禁静默
   删除事实后发布。

冻结结果整体写入 SQLite 快照：新的领退、退料或分类修订都不改变已冻结矩阵。
研究范围限定每张表最多 4×4、每个行列合计至多 200。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .classification import ClassificationStore
from .model import (
    ClassificationGapError,
    StructuralZeroViolation,
    ValidationError,
)
from .warehouse import Warehouse

__all__ = [
    "FrozenSlice",
    "OrderContribution",
    "SliceOffice",
    "build_slice",
    "MAX_ROWS",
    "MAX_COLS",
    "MAX_MARGIN",
]

MAX_ROWS = 4
MAX_COLS = 4
MAX_MARGIN = 200

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sl_slices (
    slice_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    boundary_id  INTEGER NOT NULL,
    class_rev    INTEGER NOT NULL,
    k            INTEGER NOT NULL,
    note         TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    rows_json    TEXT NOT NULL,
    cols_json    TEXT NOT NULL,
    zeros_json   TEXT NOT NULL,
    UNIQUE(boundary_id, class_rev, k, zeros_json)
);
CREATE TABLE IF NOT EXISTS sl_cells (
    slice_id     INTEGER NOT NULL,
    row_name     TEXT NOT NULL,
    col_name     TEXT NOT NULL,
    grams        INTEGER NOT NULL,
    contributors INTEGER NOT NULL,
    PRIMARY KEY (slice_id, row_name, col_name)
);
CREATE TABLE IF NOT EXISTS sl_order_cells (
    slice_id     INTEGER NOT NULL,
    order_id     TEXT NOT NULL,
    row_name     TEXT NOT NULL,
    col_name     TEXT NOT NULL,
    net_g        INTEGER NOT NULL
);
"""


# ------------------------------------------------------------ 只读数据结构


@dataclass(frozen=True)
class OrderContribution:
    """内网核对用：单张订单在单格的冻结净领克数（不对外披露）。"""

    order_id: str
    row: str
    col: str
    net_g: int


@dataclass(frozen=True)
class FrozenSlice:
    """冻结切面的完整内部视图（统计端内网使用，含精确克数与贡献者计数）。"""

    slice_id: int
    boundary_id: int
    class_rev: int
    k: int
    rows: tuple[str, ...]
    cols: tuple[str, ...]
    matrix: Mapping  # dict[(row, col) -> 非负整数克数]，含普通零格
    contributors: Mapping  # dict[(row, col) -> 正净量订单数]
    structural_zeros: frozenset[tuple[str, str]]
    protected: frozenset[tuple[str, str]]
    row_totals: Mapping  # dict[row -> int]
    col_totals: Mapping  # dict[col -> int]
    note: str
    created_at: str

    def grams(self, row: str, col: str) -> int:
        return self.matrix[(row, col)]

    def is_structural_zero(self, row: str, col: str) -> bool:
        return (row, col) in self.structural_zeros

    def is_protected(self, row: str, col: str) -> bool:
        return (row, col) in self.protected

    def public_view(self) -> dict:
        """对外固定信息：行列合计、分类维表与结构性零；不含任何格内克数。"""
        return {
            "slice_id": self.slice_id,
            "boundary_id": self.boundary_id,
            "class_rev": self.class_rev,
            "rows": list(self.rows),
            "cols": list(self.cols),
            "row_totals": {r: self.row_totals[r] for r in self.rows},
            "col_totals": {c: self.col_totals[c] for c in self.cols},
            "structural_zeros": sorted(map(list, self.structural_zeros)),
        }


# ------------------------------------------------------------ 纯函数切面构造


def _validate_names(rows, cols, structural_zeros) -> frozenset[tuple[str, str]]:
    row_set, col_set = set(rows), set(cols)
    zeros: set[tuple[str, str]] = set()
    for cell in structural_zeros:
        try:
            r, c = cell
        except (TypeError, ValueError) as exc:
            raise ValidationError("结构性零必须是 (部门, 物料族) 二元组") from exc
        if not isinstance(r, str) or not isinstance(c, str) or not r or not c:
            raise ValidationError("结构性零的行列名必须是非空字符串")
        if r not in row_set:
            raise ValidationError(f"结构性零引用了分类中不存在的部门 {r!r}")
        if c not in col_set:
            raise ValidationError(f"结构性零引用了分类中不存在的物料族 {c!r}")
        if (r, c) in zeros:
            raise ValidationError(f"结构性零清单存在重复格 ({r},{c})")
        zeros.add((r, c))
    return frozenset(zeros)


def build_slice(
    facts,
    order_departments: Mapping[str, str],
    lot_families: Mapping[str, str],
    structural_zeros,
    k: int,
) -> dict:
    """纯函数：事实 + 分类快照 -> 冻结矩阵的全部要素（不落库、不依赖 SQLite）。

    返回 dict（matrix/contributors/protected/totals/order_nets/...），
    供 :class:`SliceOffice` 持久化，也便于单测独立核对。可能抛出
    :class:`ClassificationGapError`、:class:`StructuralZeroViolation`、
    :class:`ValidationError`。
    """
    if not isinstance(k, int) or k <= 0:
        raise ValidationError("支持门槛 k 必须是正整数")
    facts = tuple(facts)

    # 分类维表取修订内的完整宇宙（含本期没有任何领退的部门/物料族）
    rows = tuple(sorted(set(order_departments.values())))
    cols = tuple(sorted(set(lot_families.values())))
    zeros = _validate_names(rows, cols, structural_zeros)
    if len(rows) > MAX_ROWS or len(cols) > MAX_COLS:
        raise ValidationError(
            f"切面规模超出研究范围：{len(rows)}×{len(cols)}，上限 {MAX_ROWS}×{MAX_COLS}"
        )

    # ---- 分类完整性：边界内每个事实的订单与 lot 都必须已分派 -------------
    seen_orders = {f.order_id for f in facts}
    seen_lots = {f.lot_id for f in facts}
    missing_orders = sorted(o for o in seen_orders if o not in order_departments)
    missing_lots = sorted(lot for lot in seen_lots if lot not in lot_families)
    if missing_orders or missing_lots:
        raise ClassificationGapError(missing_orders, missing_lots, rev=None)

    # ---- 每订单每格净量：ISSUE 正、RETURN 负 -----------------------------
    # net[(order, row, col)]；同一订单在同一格的多次领/退合并计数。
    net: dict[tuple[str, str, str], int] = {}
    for f in facts:
        if f.event not in ("ISSUE", "RETURN"):
            raise ValidationError(f"切面只接受 ISSUE/RETURN 事实，收到 {f.event}")
        row = order_departments[f.order_id]
        col = lot_families[f.lot_id]
        key = (f.order_id, row, col)
        net[key] = net.get(key, 0) + f.signed_g
    bad = {key: v for key, v in net.items() if v < 0}
    if bad:  # 仓库状态机保证退回不超过已领，这里防御性兜底
        raise ValidationError(f"存在负净领克数（退回超过领出）: {sorted(bad.items())}")

    # ---- 结构性不可能组合：有正净量落入即整体失败 -----------------------
    violations = sorted({(r, c) for (_o, r, c), v in net.items() if v > 0 and (r, c) in zeros})
    if violations:
        raise StructuralZeroViolation(violations)

    # ---- 格级聚合：累计净量 + 正贡献者计数 ------------------------------
    matrix: dict[tuple[str, str], int] = {(r, c): 0 for r in rows for c in cols}
    contributors: dict[tuple[str, str], int] = {(r, c): 0 for r in rows for c in cols}
    order_cells: list[tuple[str, str, str, int]] = []
    for (order_id, r, c), v in net.items():
        matrix[(r, c)] += v
        if v > 0:  # 全部退回（v==0）退出贡献者计数；同一订单天然只累一次
            contributors[(r, c)] += 1
            order_cells.append((order_id, r, c, v))

    row_totals = {r: sum(matrix[(r, c)] for c in cols) for r in rows}
    col_totals = {c: sum(matrix[(r, c)] for r in rows) for c in cols}
    for r, t in row_totals.items():
        if t > MAX_MARGIN:
            raise ValidationError(f"部门 {r!r} 行合计 {t} 超出研究范围上限 {MAX_MARGIN}")
    for c, t in col_totals.items():
        if t > MAX_MARGIN:
            raise ValidationError(f"物料族 {c!r} 列合计 {t} 超出研究范围上限 {MAX_MARGIN}")

    # ---- 主保护格：非空且正贡献者 < k（普通零格不在内）------------------
    protected = frozenset(
        cell for cell, g in matrix.items() if g > 0 and contributors[cell] < k
    )
    order_cells.sort()
    return {
        "rows": rows,
        "cols": cols,
        "matrix": matrix,
        "contributors": contributors,
        "structural_zeros": zeros,
        "protected": protected,
        "row_totals": row_totals,
        "col_totals": col_totals,
        "order_nets": tuple(order_cells),
    }


# ------------------------------------------------------------ SQLite 冻结库


class SliceOffice:
    """冻结切面的 SQLite 持久化与内外两套读取入口。

    与履约仓、分类库可以是三个独立文件；切面库只存冻结快照，重开后
    已冻结矩阵不随后续领退或分类修订变化。
    """

    def __init__(
        self,
        db_path: str | Path = ":memory:",
        busy_timeout_ms: int = 10000,
    ) -> None:
        self.path = str(db_path)
        self._conn = sqlite3.connect(self.path)
        self._conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SliceOffice":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ======================================================== 冻结
    def freeze_from(
        self,
        warehouse: Warehouse,
        classification: ClassificationStore,
        *,
        k: int,
        structural_zeros=(),
        class_rev: int | None = None,
        boundary_id: int | None = None,
        note: str = "",
    ) -> int:
        """从仓库事实与分类修订冻结一个新切面，返回 slice_id。

        * ``boundary_id`` 缺省取仓库当前最大台账号，即固定到此为止的事件边界；
        * ``class_rev`` 缺省取分类库当前修订；
        * 分类缺口、结构性零冲突、超范围都在写入前抛出，库内不留半成品。
        """
        if boundary_id is None:
            boundary_id = warehouse.max_ledger_id()
        if not isinstance(boundary_id, int) or boundary_id < 0:
            raise ValidationError("事件边界必须是非负整数")
        facts = warehouse.issue_facts(boundary_id)
        rev = classification.revision if class_rev is None else class_rev
        order_map = classification.order_departments(rev)
        lot_map = classification.lot_families(rev)

        try:
            built = build_slice(facts, order_map, lot_map, structural_zeros, k)
        except ClassificationGapError as exc:
            # 纯函数层没有修订上下文，这里补上实际绑定的分类修订号
            raise ClassificationGapError(
                exc.missing_orders, exc.missing_lots, rev
            ) from exc
        zeros_json = json.dumps(sorted(map(list, built["structural_zeros"])),
                                ensure_ascii=False, sort_keys=True)
        now = datetime.now(timezone.utc).isoformat()
        conn = self._conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            dup = conn.execute(
                "SELECT 1 FROM sl_slices WHERE boundary_id=? AND class_rev=? AND k=?"
                " AND zeros_json=?",
                (boundary_id, rev, k, zeros_json),
            ).fetchone()
            if dup:
                raise ValidationError(
                    f"相同边界 {boundary_id}/分类 rev{rev}/门槛 k{k} 的切面已存在，"
                    "冻结输入不变时不必重复发布"
                )
            cur = conn.execute(
                "INSERT INTO sl_slices(boundary_id, class_rev, k, note, created_at,"
                " rows_json, cols_json, zeros_json) VALUES (?,?,?,?,?,?,?,?)",
                (
                    boundary_id, rev, k, note, now,
                    json.dumps(built["rows"], ensure_ascii=False),
                    json.dumps(built["cols"], ensure_ascii=False),
                    zeros_json,
                ),
            )
            slice_id = int(cur.lastrowid)
            conn.executemany(
                "INSERT INTO sl_cells(slice_id, row_name, col_name, grams, contributors)"
                " VALUES (?,?,?,?,?)",
                [
                    (slice_id, r, c, built["matrix"][(r, c)], built["contributors"][(r, c)])
                    for r in built["rows"] for c in built["cols"]
                ],
            )
            conn.executemany(
                "INSERT INTO sl_order_cells(slice_id, order_id, row_name, col_name, net_g)"
                " VALUES (?,?,?,?,?)",
                [(slice_id, *t) for t in built["order_nets"]],
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return slice_id

    # ======================================================== 内网读取（精确）
    def list_slices(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT slice_id, boundary_id, class_rev, k, note, created_at"
            " FROM sl_slices ORDER BY slice_id"
        ).fetchall()
        return [
            {"slice_id": r[0], "boundary_id": r[1], "class_rev": r[2],
             "k": r[3], "note": r[4], "created_at": r[5]}
            for r in rows
        ]

    def get_slice(self, slice_id: int) -> FrozenSlice:
        """统计端内网入口：完整精确矩阵、贡献者计数与主保护格清单。"""
        head = self._conn.execute(
            "SELECT boundary_id, class_rev, k, note, created_at, rows_json, cols_json,"
            " zeros_json FROM sl_slices WHERE slice_id=?",
            (slice_id,),
        ).fetchone()
        if head is None:
            raise ValidationError(f"切面 {slice_id} 不存在")
        boundary_id, rev, k, note, created_at, rows_j, cols_j, zeros_j = head
        rows = tuple(json.loads(rows_j))
        cols = tuple(json.loads(cols_j))
        zeros = frozenset(tuple(x) for x in json.loads(zeros_j))
        cell_rows = self._conn.execute(
            "SELECT row_name, col_name, grams, contributors FROM sl_cells"
            " WHERE slice_id=? ORDER BY row_name, col_name",
            (slice_id,),
        ).fetchall()
        matrix: dict[tuple[str, str], int] = {}
        contributors: dict[tuple[str, str], int] = {}
        for r, c, g, n in cell_rows:
            matrix[(r, c)] = g
            contributors[(r, c)] = n
        row_totals = {r: sum(matrix[(r, c)] for c in cols) for r in rows}
        col_totals = {c: sum(matrix[(r, c)] for r in rows) for c in cols}
        protected = frozenset(
            cell for cell, g in matrix.items() if g > 0 and contributors[cell] < k
        )
        return FrozenSlice(
            slice_id=slice_id,
            boundary_id=boundary_id,
            class_rev=rev,
            k=k,
            rows=rows,
            cols=cols,
            matrix=matrix,
            contributors=contributors,
            structural_zeros=zeros,
            protected=protected,
            row_totals=row_totals,
            col_totals=col_totals,
            note=note,
            created_at=created_at,
        )

    def get_order_contributions(self, slice_id: int) -> tuple[OrderContribution, ...]:
        """内网核对：逐订单逐格的冻结净量（贡献者计数可由此独立重算）。"""
        self._require_slice(slice_id)
        rows = self._conn.execute(
            "SELECT order_id, row_name, col_name, net_g FROM sl_order_cells"
            " WHERE slice_id=? ORDER BY order_id, row_name, col_name",
            (slice_id,),
        ).fetchall()
        return tuple(OrderContribution(*r) for r in rows)

    # ======================================================== 对外读取（脱敏）
    def public_view(self, slice_id: int) -> dict:
        """对外固定公开：行列合计、分类维表、结构性零；绝不含格内克数/人数。"""
        return self.get_slice(slice_id).public_view()

    def _require_slice(self, slice_id: int) -> None:
        if self._conn.execute(
            "SELECT 1 FROM sl_slices WHERE slice_id=?", (slice_id,)
        ).fetchone() is None:
            raise ValidationError(f"切面 {slice_id} 不存在")
