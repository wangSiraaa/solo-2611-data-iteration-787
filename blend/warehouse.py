"""履约仓：预览不占料、确认事务锁、领料/退回/取消全流程。

与质量目录解耦：

* 规划走 :class:`~blend.planner.Planner`，预览只保存方案快照与目录修订号，
  **不占用任何库存**；
* 确认在单个 ``BEGIN IMMEDIATE`` 事务内完成「版本核对 → 库存核对 → 占料落账」，
  两个独立连接不可能同时花掉同一份最后库存（SQLite 写锁串行化 + 余量复核）；
* 若确认时目录修订号已变化，直接抛 :class:`StalePreviewError` 拒绝，
  绝不把旧质量方案套到新目录上；
* 也接受外部 :class:`~blend.model.UsageProduct`（``preview_external``），
  仓库只认质量产品与用量，不绑定具体目录实现。

实物状态机（每条 allocation = 订单 × lot 一行）::

    PENDING（已占未领） --issue-->  PARTIAL_ISSUED/ISSUED
    PENDING            --cancel--> RELEASED（份额直接回到可用库存）
    已领出份额           --return--> PARTIAL_RETURNED/RETURNED（退回入库，另立台账）

取消只释放尚未领出的份额；已领出的配料不能复活成“从未使用”，
只能登记退回，退回与领料分别留痕。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Mapping, Sequence

from .catalog import QualityCatalog
from .model import (
    ComponentConstraint,
    ConflictError,
    Order,
    PlanResult,
    StateError,
    StalePreviewError,
    UsageProduct,
    ValidationError,
)
from .planner import Planner, active_count, usage_cost

__all__ = ["Warehouse", "OrderView", "AllocationView", "LedgerEntry"]

# allocation 状态
ST_PENDING = "PENDING"
ST_PARTIAL_ISSUED = "PARTIAL_ISSUED"
ST_ISSUED = "ISSUED"
ST_PARTIAL_RETURNED = "PARTIAL_RETURNED"
ST_RETURNED = "RETURNED"
ST_RELEASED = "RELEASED"

# 订单/预览状态
O_CONFIRMED = "CONFIRMED"
O_FULLY_ISSUED = "FULLY_ISSUED"
O_CANCELLED = "CANCELLED"
PV_PLANNED = "PLANNED"
PV_CONFIRMED = "CONFIRMED"
PV_STALE = "REJECTED_STALE"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS wh_lots (
    lot_id     TEXT PRIMARY KEY,
    step_g     INTEGER NOT NULL,
    cost_per_g INTEGER NOT NULL,
    fixed_fee  INTEGER NOT NULL,
    initial_g  INTEGER NOT NULL,
    allocated_g INTEGER NOT NULL DEFAULT 0,   -- 已确认占用、未领出
    issued_g   INTEGER NOT NULL DEFAULT 0,    -- 累计实际领出
    returned_g INTEGER NOT NULL DEFAULT 0     -- 累计退回入库
);
CREATE TABLE IF NOT EXISTS wh_previews (
    pv_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id     TEXT NOT NULL,
    created_rev  INTEGER,
    quality_src  TEXT NOT NULL,
    status       TEXT NOT NULL,
    plan_json    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wh_orders (
    order_id     TEXT PRIMARY KEY,
    pv_id        INTEGER NOT NULL,
    confirmed_rev INTEGER,
    quality_src  TEXT NOT NULL,
    status       TEXT NOT NULL,
    total_g      INTEGER NOT NULL,
    total_cost   INTEGER NOT NULL,
    plan_json    TEXT NOT NULL,
    confirmed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wh_allocations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id     TEXT NOT NULL,
    lot_id       TEXT NOT NULL,
    grams        INTEGER NOT NULL,
    state        TEXT NOT NULL,
    pending_g    INTEGER NOT NULL,
    issued_g     INTEGER NOT NULL DEFAULT 0,
    returned_g   INTEGER NOT NULL DEFAULT 0,
    UNIQUE(order_id, lot_id)
);
CREATE TABLE IF NOT EXISTS wh_ledger (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id  TEXT NOT NULL,
    lot_id    TEXT NOT NULL,
    event     TEXT NOT NULL,
    grams     INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    detail    TEXT NOT NULL DEFAULT ''
);
"""


# ------------------------------------------------------------ JSON 序列化


def _frac_str(x: Fraction) -> str:
    return f"{x.numerator}/{x.denominator}"


def _frac(s: str) -> Fraction:
    return Fraction(s)


def _dump_plan(
    order: Order,
    usage: Mapping[str, int],
    quality: UsageProduct,
    total_cost: int,
) -> str:
    payload = {
        "order_id": order.order_id,
        "total_g": order.total_g,
        "max_lots": order.max_lots,
        "constraints": [
            {"component": c.component, "lo": _frac_str(c.lo), "hi": _frac_str(c.hi)}
            for c in order.constraints
        ],
        "usage": dict(usage),
        "quality": {
            "source": quality.source,
            "total_g": quality.total_g,
            "mass_bounds": {
                k: [_frac_str(a), _frac_str(b)]
                for k, (a, b) in quality.mass_bounds.items()
            },
            "fraction_bounds": {
                k: [_frac_str(a), _frac_str(b)]
                for k, (a, b) in quality.fraction_bounds.items()
            },
            "components": sorted(quality.components),
        },
        "total_cost": total_cost,
        "active_lot_count": active_count(usage),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _load_plan(blob: str) -> dict:
    return json.loads(blob)


# ------------------------------------------------------------ 只读视图


@dataclass(frozen=True)
class AllocationView:
    order_id: str
    lot_id: str
    grams: int
    state: str
    pending_g: int
    issued_g: int
    returned_g: int


@dataclass(frozen=True)
class LedgerEntry:
    ledger_id: int
    order_id: str
    lot_id: str
    event: str
    grams: int
    created_at: str
    detail: str


@dataclass(frozen=True)
class OrderView:
    order_id: str
    status: str
    total_g: int
    total_cost: int
    confirmed_rev: int | None
    quality_src: str
    plan: dict
    allocations: tuple[AllocationView, ...]
    ledger: tuple[LedgerEntry, ...]


# ------------------------------------------------------------ 仓库主体


class Warehouse:
    """SQLite 履约仓。

    Parameters
    ----------
    db_path:
        仓库数据库文件；``":memory:"`` 为内存库。重开文件库后订单、
        占料、领料与退回台账完整保持。
    catalog:
        绑定的质量目录，用于规划与确认时的版本核对；纯外部产品流程
        可传 ``None``。
    """

    def __init__(
        self,
        db_path: str | Path = ":memory:",
        catalog: QualityCatalog | None = None,
        busy_timeout_ms: int = 10000,
    ) -> None:
        self.path = str(db_path)
        self.catalog = catalog
        # check_same_thread=False：演示并发争料时允许多线程共享连接；
        # 写操作仍由 BEGIN IMMEDIATED 的库级写锁串行化。
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Warehouse":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ======================================================== 库存目录
    def register_lots(self, lots) -> None:
        """把 lot 的物料档案与初始库存登记进仓库（幂等 upsert 档案）。

        已存在 lot 的物料数量（initial/占料/领料/退回）不被覆盖，
        只刷新步长与成本档案；新 lot 以其 available_g 作为初始库存。
        """
        for lot in lots:
            row = self._conn.execute(
                "SELECT lot_id FROM wh_lots WHERE lot_id=?", (lot.lot_id,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO wh_lots(lot_id, step_g, cost_per_g, fixed_fee, initial_g)"
                    " VALUES (?,?,?,?,?)",
                    (lot.lot_id, lot.step_g, lot.cost_per_g, lot.fixed_fee, lot.available_g),
                )
            else:
                self._conn.execute(
                    "UPDATE wh_lots SET step_g=?, cost_per_g=?, fixed_fee=? WHERE lot_id=?",
                    (lot.step_g, lot.cost_per_g, lot.fixed_fee, lot.lot_id),
                )
        self._conn.commit()

    def inventory(self) -> list[dict]:
        """当前库存快照：初始/占用/已领/退回与即时可用克数。"""
        rows = self._conn.execute(
            "SELECT lot_id, step_g, cost_per_g, fixed_fee, initial_g,"
            " allocated_g, issued_g, returned_g FROM wh_lots ORDER BY lot_id"
        ).fetchall()
        out = []
        for lot_id, step, cpg, fee, init, alloc, issued, returned in rows:
            out.append(
                {
                    "lot_id": lot_id,
                    "step_g": step,
                    "cost_per_g": cpg,
                    "fixed_fee": fee,
                    "initial_g": init,
                    "allocated_g": alloc,
                    "issued_g": issued,
                    "returned_g": returned,
                    # 可用 = 初始 - 占用 - 已领 + 已退回
                    "available_g": init - alloc - issued + returned,
                }
            )
        return out

    def available_g(self, lot_id: str) -> int:
        row = self._conn.execute(
            "SELECT initial_g, allocated_g, issued_g, returned_g FROM wh_lots WHERE lot_id=?",
            (lot_id,),
        ).fetchone()
        if row is None:
            raise ValidationError(f"仓库未登记 lot: {lot_id!r}")
        init, alloc, issued, returned = row
        return init - alloc - issued + returned

    # ======================================================== 规划与预览
    def plan_order(self, order: Order) -> PlanResult:
        """按目录当前修订规划（不落库、不占料），返回最优整数方案。"""
        if self.catalog is None:
            raise ValidationError("仓库未绑定质量目录，无法直接规划；请用 preview_external")
        lots = self.catalog.get_lots()
        return Planner(order, lots).solve()

    def preview_order(self, order: Order) -> int:
        """规划并保存预览，返回 pv_id。预览绑定当前目录修订号，不占料。"""
        result = self.plan_order(order)
        rev = self.catalog.revision
        blob = _dump_plan(order, result.quality.usage, result.quality, result.total_cost)
        cur = self._conn.execute(
            "INSERT INTO wh_previews(order_id, created_rev, quality_src, status, plan_json)"
            " VALUES (?,?,?,?,?)",
            (order.order_id, rev, result.quality.source, PV_PLANNED, blob),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def preview_external(
        self,
        order: Order,
        result: PlanResult,
    ) -> int:
        """消费外部规划结果（外部质量产品）生成预览。

        仓库不重新解释质量，只原样保存产品快照；确认时不做目录修订号
        绑定校验（``confirmed_rev`` 记 NULL），但库存/步长/总量照核。
        """
        if result.total_g != order.total_g:
            raise ValidationError("外部方案总克数与订单 M 不一致")
        blob = _dump_plan(order, result.quality.usage, result.quality, result.total_cost)
        cur = self._conn.execute(
            "INSERT INTO wh_previews(order_id, created_rev, quality_src, status, plan_json)"
            " VALUES (?,?,?,?,?)",
            (order.order_id, None, result.quality.source, PV_PLANNED, blob),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def get_preview(self, pv_id: int) -> dict:
        row = self._conn.execute(
            "SELECT pv_id, order_id, created_rev, quality_src, status, plan_json"
            " FROM wh_previews WHERE pv_id=?",
            (pv_id,),
        ).fetchone()
        if row is None:
            raise ValidationError(f"预览 {pv_id} 不存在")
        return {
            "pv_id": row[0],
            "order_id": row[1],
            "created_rev": row[2],
            "quality_src": row[3],
            "status": row[4],
            "plan": _load_plan(row[5]),
        }

    # ======================================================== 确认（事务锁）
    def confirm_preview(self, pv_id: int) -> str:
        """正式确认预览，返回订单号。

        全程在一个 ``BEGIN IMMEDIATE`` 事务内：

        1. 预览状态与目录修订号核对（换代预览直接拒绝）；
        2. 逐 lot 复核即时可用库存（在写锁保护下，杜绝并发双花）；
        3. 写入订单、PENDING 占料与台账并提交。

        任一步失败整体回滚，库存与状态保持确认前原样。
        """
        conn = self._conn
        pv = self._get_preview_row(pv_id)
        order_id, created_rev, quality_src, status, blob = pv
        if status != PV_PLANNED:
            raise StateError(f"预览 {pv_id} 状态为 {status}，不能重复确认")
        plan = _load_plan(blob)
        usage: dict[str, int] = plan["usage"]

        stale = False
        conn.execute("BEGIN IMMEDIATE")
        try:
            # ---- 版本核对：目录修订必须与预览绑定时一致 ----------------
            if created_rev is not None:
                if self.catalog is None:
                    raise ValidationError("目录预览需要绑定目录才能确认")
                current_rev = self.catalog.revision
                if current_rev != created_rev:
                    # 不在事务内改任何业务表；整体回滚后再在事务外标记
                    stale = True
                    raise StalePreviewError(
                        f"预览 {pv_id} 绑定 rev{created_rev}，目录已修订到 "
                        f"rev{current_rev}，拒绝确认，请按新质量重新规划"
                    )

            # ---- 订单唯一性 -------------------------------------------
            if conn.execute(
                "SELECT 1 FROM wh_orders WHERE order_id=?", (order_id,)
            ).fetchone():
                raise StateError(f"订单 {order_id} 已存在确认记录")

            # ---- 真实库存核对（写锁内，读最新行） ----------------------
            for lot_id, grams in usage.items():
                if grams <= 0:
                    continue
                row = conn.execute(
                    "SELECT initial_g, allocated_g, issued_g, returned_g, step_g"
                    " FROM wh_lots WHERE lot_id=?",
                    (lot_id,),
                ).fetchone()
                if row is None:
                    raise ValidationError(f"仓库未登记 lot: {lot_id!r}")
                init, alloc, issued, returned, step = row
                available = init - alloc - issued + returned
                if grams > available:
                    raise ConflictError(
                        f"lot {lot_id} 需领 {grams} g，事务内可用仅 {available} g"
                        "（可能已被其他确认占用）"
                    )
                if grams % step != 0:
                    raise ValidationError(f"lot {lot_id} 用量 {grams} 不是步长 {step} 的倍数")

            # ---- 落账：订单 + PENDING 占料 + 台账 ----------------------
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO wh_orders(order_id, pv_id, confirmed_rev, quality_src,"
                " status, total_g, total_cost, plan_json, confirmed_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    order_id,
                    pv_id,
                    created_rev,
                    quality_src,
                    O_CONFIRMED,
                    plan["total_g"],
                    plan["total_cost"],
                    blob,
                    now,
                ),
            )
            for lot_id, grams in sorted(usage.items()):
                if grams <= 0:
                    continue
                conn.execute(
                    "INSERT INTO wh_allocations"
                    "(order_id, lot_id, grams, state, pending_g)"
                    " VALUES (?,?,?,?,?)",
                    (order_id, lot_id, grams, ST_PENDING, grams),
                )
                conn.execute(
                    "UPDATE wh_lots SET allocated_g = allocated_g + ? WHERE lot_id=?",
                    (grams, lot_id),
                )
                conn.execute(
                    "INSERT INTO wh_ledger(order_id, lot_id, event, grams, created_at, detail)"
                    " VALUES (?,?,?,?,?,?)",
                    (order_id, lot_id, "CONFIRM", grams, now, f"pv={pv_id} rev={created_rev}"),
                )
            conn.execute(
                "UPDATE wh_previews SET status=? WHERE pv_id=?",
                (PV_CONFIRMED, pv_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            if stale:
                # 事务已干净回滚（库存分毫未动），再在自动提交模式标记预览失效
                self._conn.execute(
                    "UPDATE wh_previews SET status=? WHERE pv_id=?",
                    (PV_STALE, pv_id),
                )
                self._conn.commit()
            raise
        return order_id

    def _get_preview_row(self, pv_id: int):
        row = self._conn.execute(
            "SELECT order_id, created_rev, quality_src, status, plan_json"
            " FROM wh_previews WHERE pv_id=?",
            (pv_id,),
        ).fetchone()
        if row is None:
            raise ValidationError(f"预览 {pv_id} 不存在")
        return row

    # ======================================================== 领料
    def issue(self, order_id: str, lot_id: str, grams: int) -> None:
        """实际领料：把 PENDING 份额转成已领出，写领料台账。

        只能领该订单该 lot 尚未领出的占用份额；不足或状态不对都拒绝，
        事务整体回滚。
        """
        self._positive_int(grams, "领料克数")
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            order = self._order_row(conn, order_id)
            if order["status"] == O_CANCELLED:
                raise StateError(f"订单 {order_id} 已取消，不能再领料")
            alloc = self._alloc_row(conn, order_id, lot_id)
            if alloc["pending_g"] < grams:
                raise ConflictError(
                    f"订单 {order_id} lot {lot_id} 待领仅 {alloc['pending_g']} g，"
                    f"无法领 {grams} g"
                )
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc).isoformat()
            new_pending = alloc["pending_g"] - grams
            new_issued = alloc["issued_g"] + grams
            conn.execute(
                "UPDATE wh_allocations SET pending_g=?, issued_g=?, state=? WHERE id=?",
                (new_pending, new_issued, self._issued_state(new_pending, new_issued), alloc["id"]),
            )
            # 实物离开可用库存：占用减少、已领增加（available 同步下降）
            conn.execute(
                "UPDATE wh_lots SET allocated_g = allocated_g - ?, issued_g = issued_g + ?"
                " WHERE lot_id=?",
                (grams, grams, lot_id),
            )
            conn.execute(
                "INSERT INTO wh_ledger(order_id, lot_id, event, grams, created_at, detail)"
                " VALUES (?,?,?,?,?,?)",
                (order_id, lot_id, "ISSUE", grams, now, ""),
            )
            self._refresh_order_status(conn, order_id)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # ======================================================== 退回
    def return_material(self, order_id: str, lot_id: str, grams: int) -> None:
        """登记退回：只允许退回已实际领出的份额，退回品入库可再次使用。

        退回不抹销领料历史——ledger 里 ISSUE 与 RETURN 各留一行；
        库存口径上 returned_g 增加，available 随之回升。
        """
        self._positive_int(grams, "退回克数")
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            alloc = self._alloc_row(conn, order_id, lot_id)
            returnable = alloc["issued_g"] - alloc["returned_g"]
            if grams > returnable:
                raise ConflictError(
                    f"lot {lot_id} 可退回（已领未退）仅 {returnable} g，无法退 {grams} g"
                )
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc).isoformat()
            new_returned = alloc["returned_g"] + grams
            conn.execute(
                "UPDATE wh_allocations SET returned_g=?, state=? WHERE id=?",
                (new_returned, self._returned_state(alloc, new_returned), alloc["id"]),
            )
            conn.execute(
                "UPDATE wh_lots SET returned_g = returned_g + ? WHERE lot_id=?",
                (grams, lot_id),
            )
            conn.execute(
                "INSERT INTO wh_ledger(order_id, lot_id, event, grams, created_at, detail)"
                " VALUES (?,?,?,?,?,?)",
                (order_id, lot_id, "RETURN", grams, now, ""),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # ======================================================== 取消
    def cancel_order(self, order_id: str) -> dict:
        """取消订单：仅释放尚未实际领出的 PENDING 份额。

        已领出部分不复活、不自动回库；需要事后退回走 :meth:`return_material`。
        返回各 lot 实际释放克数。
        """
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        released: dict[str, int] = {}
        try:
            order = self._order_row(conn, order_id)
            if order["status"] == O_CANCELLED:
                raise StateError(f"订单 {order_id} 已取消，不能重复取消")
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc).isoformat()
            rows = conn.execute(
                "SELECT id, lot_id, pending_g FROM wh_allocations"
                " WHERE order_id=? AND pending_g > 0",
                (order_id,),
            ).fetchall()
            for alloc_id, lot_id, pending_g in rows:
                conn.execute(
                    "UPDATE wh_allocations SET state=?, pending_g=0 WHERE id=?",
                    (ST_RELEASED, alloc_id),
                )
                conn.execute(
                    "UPDATE wh_lots SET allocated_g = allocated_g - ? WHERE lot_id=?",
                    (pending_g, lot_id),
                )
                conn.execute(
                    "INSERT INTO wh_ledger(order_id, lot_id, event, grams, created_at, detail)"
                    " VALUES (?,?,?,?,?,?)",
                    (order_id, lot_id, "CANCEL_RELEASE", pending_g, now, ""),
                )
                released[lot_id] = pending_g
            conn.execute(
                "UPDATE wh_orders SET status=? WHERE order_id=?",
                (O_CANCELLED, order_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return released

    # ======================================================== 查询与解释
    def get_order(self, order_id: str) -> OrderView:
        conn = self._conn
        order = self._order_row(conn, order_id)
        allocs = tuple(
            AllocationView(*r)
            for r in conn.execute(
                "SELECT order_id, lot_id, grams, state, pending_g, issued_g, returned_g"
                " FROM wh_allocations WHERE order_id=? ORDER BY lot_id",
                (order_id,),
            ).fetchall()
        )
        ledger = tuple(
            LedgerEntry(*r)
            for r in conn.execute(
                "SELECT id, order_id, lot_id, event, grams, created_at, detail"
                " FROM wh_ledger WHERE order_id=? ORDER BY id",
                (order_id,),
            ).fetchall()
        )
        return OrderView(
            order_id=order_id,
            status=order["status"],
            total_g=order["total_g"],
            total_cost=order["total_cost"],
            confirmed_rev=order["confirmed_rev"],
            quality_src=order["quality_src"],
            plan=_load_plan(order["plan_json"]),
            allocations=allocs,
            ledger=ledger,
        )

    def list_orders(self) -> list[str]:
        return [r[0] for r in self._conn.execute(
            "SELECT order_id FROM wh_orders ORDER BY order_id"
        ).fetchall()]

    def explain_order(self, order_id: str) -> str:
        """解释入口：来源修订、质量边界、占用/领料/退回/取消全台账。"""
        view = self.get_order(order_id)
        plan = view.plan
        q = plan["quality"]
        lines = [
            f"订单 {view.order_id}（状态 {view.status}，来源 {view.quality_src}，"
            f"确认修订 {view.confirmed_rev}）",
            f"  总克数 {view.total_g}，总成本 {view.total_cost}，活跃 lot {plan['active_lot_count']}",
            "  确认时质量边界（分数）：",
        ]
        for comp in q["components"]:
            lo, hi = q["fraction_bounds"][comp]
            lines.append(f"    - {comp}: [{lo}, {hi}]")
        lines.append("  约束与核账：")
        for c in plan["constraints"]:
            lo, hi = q["fraction_bounds"][c["component"]]
            ok = Fraction(lo) >= Fraction(c["lo"]) and Fraction(hi) <= Fraction(c["hi"])
            lines.append(
                f"    - {c['component']}: 可达 [{lo}, {hi}] ⊆ 订单 [{c['lo']}, {c['hi']}]"
                f" -> {'稳健合格' if ok else '不合格'}"
            )
        lines.append("  物料台账：")
        for a in view.allocations:
            lines.append(
                f"    - {a.lot_id}: 占用 {a.grams} g，状态 {a.state}，"
                f"待领 {a.pending_g}，已领 {a.issued_g}，已退 {a.returned_g}"
            )
        lines.append("  事件流：")
        for e in view.ledger:
            extra = f"（{e.detail}）" if e.detail else ""
            lines.append(f"    #{e.ledger_id} {e.created_at} {e.event} {e.lot_id} {e.grams} g{extra}")
        return "\n".join(lines)

    # ======================================================== 内部辅助
    @staticmethod
    def _positive_int(grams: int, what: str) -> None:
        if not isinstance(grams, int) or grams <= 0:
            raise ValidationError(f"{what}必须是正整数")

    def _order_row(self, conn, order_id: str) -> dict:
        row = conn.execute(
            "SELECT order_id, status, total_g, total_cost, confirmed_rev, quality_src, plan_json"
            " FROM wh_orders WHERE order_id=?",
            (order_id,),
        ).fetchone()
        if row is None:
            raise ValidationError(f"订单 {order_id} 不存在")
        return {
            "order_id": row[0],
            "status": row[1],
            "total_g": row[2],
            "total_cost": row[3],
            "confirmed_rev": row[4],
            "quality_src": row[5],
            "plan_json": row[6],
        }

    def _alloc_row(self, conn, order_id: str, lot_id: str) -> dict:
        row = conn.execute(
            "SELECT id, state, pending_g, issued_g, returned_g, grams"
            " FROM wh_allocations WHERE order_id=? AND lot_id=?",
            (order_id, lot_id),
        ).fetchone()
        if row is None:
            raise ValidationError(f"订单 {order_id} 没有 lot {lot_id} 的占用记录")
        return {
            "id": row[0],
            "state": row[1],
            "pending_g": row[2],
            "issued_g": row[3],
            "returned_g": row[4],
            "grams": row[5],
        }

    @staticmethod
    def _issued_state(pending_g: int, issued_g: int) -> str:
        if pending_g == 0:
            return ST_ISSUED
        return ST_PARTIAL_ISSUED

    @staticmethod
    def _returned_state(alloc: dict, new_returned: int) -> str:
        if new_returned >= alloc["issued_g"]:
            return ST_RETURNED
        return ST_PARTIAL_RETURNED

    def _refresh_order_status(self, conn, order_id: str) -> None:
        row = conn.execute(
            "SELECT COALESCE(SUM(pending_g),0), COALESCE(SUM(issued_g),0),"
            " COALESCE(SUM(grams),0) FROM wh_allocations WHERE order_id=?",
            (order_id,),
        ).fetchone()
        pending, issued, total = row
        if pending == 0 and issued == total and total > 0:
            conn.execute(
                "UPDATE wh_orders SET status=? WHERE order_id=?",
                (O_FULLY_ISSUED, order_id),
            )
