"""独立原料质量目录（SQLite 持久化，精确有理数端点）。

目录职责：

1. 维护每个 lot 的稳定 lot_id、非负整数可用克数、包装步长/成本，以及若干
   成分的质量分数闭区间 ``[lo, hi]``（端点为精确有理数，``0<=lo<=hi<=1``）；
2. 支持一次整体修订（区间与可用量同时改），修订在单个事务内提交，
   失败则目录整体保持修订前状态，旧修订号永久可查；
3. 对任意给定用量组合独立计算每成分的质量和上下界与分数上下界，
   以 :class:`~blend.model.UsageProduct` 形式公开产品；
4. 不感知订单、不处理优先级，可被规划器与仓库独立调用。

区间语义（独立区间算术）：同批每克的同一未知成分分数在 ``[lo,hi]`` 内任取，
成分之间不声明相关性；组合质量和的可达区间为各 lot 贡献区间的 Minkowski 和，
即上下界分别线性求和。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .model import (
    ComponentConstraint,
    Lot,
    UsageProduct,
    ValidationError,
)

__all__ = ["QualityCatalog"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cat_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cat_revision (
    rev        INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    note       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cat_lot_rev (
    rev        INTEGER NOT NULL,
    lot_id     TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    available_g INTEGER NOT NULL,
    step_g     INTEGER NOT NULL,
    cost_per_g INTEGER NOT NULL,
    fixed_fee  INTEGER NOT NULL,
    PRIMARY KEY (rev, lot_id)
);
CREATE TABLE IF NOT EXISTS cat_interval_rev (
    rev       INTEGER NOT NULL,
    lot_id    TEXT NOT NULL,
    component TEXT NOT NULL,
    lo_num    INTEGER NOT NULL,
    lo_den    INTEGER NOT NULL,
    hi_num    INTEGER NOT NULL,
    hi_den    INTEGER NOT NULL,
    PRIMARY KEY (rev, lot_id, component)
);
"""


class QualityCatalog:
    """SQLite 支持的原料质量目录。

    Parameters
    ----------
    path:
        数据库文件路径；``":memory:"`` 为内存目录。文件库的状态在重开
        进程后完整保持。
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._conn = sqlite3.connect(self.path)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------ 生命周期
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "QualityCatalog":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------ 内部工具
    def _current_rev(self) -> int | None:
        row = self._conn.execute(
            "SELECT value FROM cat_meta WHERE key = 'current_rev'"
        ).fetchone()
        return None if row is None else int(row[0])

    @property
    def revision(self) -> int | None:
        """当前修订号；尚无任何修订时为 ``None``。"""
        return self._current_rev()

    def revisions(self) -> list[tuple[int, str, str]]:
        """全部历史修订 ``(rev, created_at, note)``，按修订号升序。"""
        return list(
            self._conn.execute(
                "SELECT rev, created_at, note FROM cat_revision ORDER BY rev"
            )
        )

    # ------------------------------------------------------------ 修订
    def replace_lots(self, lots: Iterable[Lot], note: str = "") -> int:
        """整体修订：用新 lot 集合替换当前目录，返回新修订号。

        校验与写入在单事务内完成；任何 lot 不合法、lot_id 重复或成分
        词表不一致都会回滚，目录（含修订号）整体保持修订前状态。
        可用量与区间在同一次修订里一起生效，不存在中间态。
        """
        lots = list(lots)
        # ---- 事务外先做纯数据校验，事务内只做落库 -------------------
        ids: list[str] = []
        component_sets: set[frozenset[str]] = set()
        for lot in lots:
            if not isinstance(lot, Lot):
                raise ValidationError("replace_lots 仅接受 Lot 对象")
            ids.append(lot.lot_id)
            component_sets.add(frozenset(lot.intervals))
        if len(ids) != len(set(ids)):
            dup = sorted({x for x in ids if ids.count(x) > 1})
            raise ValidationError(f"lot_id 重复: {dup}")
        if len(component_sets) > 1:
            raise ValidationError(
                "目录内所有 lot 必须共享同一组成分词表（缺失成分请显式给 [0,0]）"
            )

        prev_rev = self._current_rev()
        new_rev = 1 if prev_rev is None else prev_rev + 1
        now = datetime.now(timezone.utc).isoformat()
        try:
            self._conn.execute("BEGIN")
            self._conn.execute(
                "INSERT INTO cat_revision(rev, created_at, note) VALUES (?,?,?)",
                (new_rev, now, note),
            )
            for seq, lot in enumerate(lots):
                self._conn.execute(
                    "INSERT INTO cat_lot_rev"
                    "(rev, lot_id, seq, available_g, step_g, cost_per_g, fixed_fee)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (
                        new_rev,
                        lot.lot_id,
                        seq,
                        lot.available_g,
                        lot.step_g,
                        lot.cost_per_g,
                        lot.fixed_fee,
                    ),
                )
                for comp, (lo, hi) in lot.intervals.items():
                    self._conn.execute(
                        "INSERT INTO cat_interval_rev"
                        "(rev, lot_id, component, lo_num, lo_den, hi_num, hi_den)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (
                            new_rev,
                            lot.lot_id,
                            comp,
                            lo.numerator,
                            lo.denominator,
                            hi.numerator,
                            hi.denominator,
                        ),
                    )
            self._conn.execute(
                "INSERT INTO cat_meta(key, value) VALUES('current_rev', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(new_rev),),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return new_rev

    # ------------------------------------------------------------ 查询
    def get_lots(self, rev: int | None = None) -> tuple[Lot, ...]:
        """取某修订（默认当前修订）的全部 lot，按修订写入顺序返回。"""
        rev = self._require_rev(rev)
        rows = self._conn.execute(
            "SELECT lot_id, available_g, step_g, cost_per_g, fixed_fee "
            "FROM cat_lot_rev WHERE rev=? ORDER BY seq",
            (rev,),
        ).fetchall()
        out: list[Lot] = []
        for lot_id, avail, step, cpg, fee in rows:
            iv_rows = self._conn.execute(
                "SELECT component, lo_num, lo_den, hi_num, hi_den "
                "FROM cat_interval_rev WHERE rev=? AND lot_id=? ORDER BY component",
                (rev, lot_id),
            ).fetchall()
            intervals = {
                comp: (Fraction(ln, ld), Fraction(hn, hd))
                for comp, ln, ld, hn, hd in iv_rows
            }
            out.append(Lot(lot_id, avail, intervals, step, cpg, fee))
        return tuple(out)

    def get_lot(self, lot_id: str, rev: int | None = None) -> Lot:
        for lot in self.get_lots(rev):
            if lot.lot_id == lot_id:
                return lot
        raise ValidationError(f"lot {lot_id!r} 在修订 {rev} 中不存在")

    def components(self, rev: int | None = None) -> tuple[str, ...]:
        """该修订的成分词表（所有 lot 共享），按名称排序。"""
        rev = self._require_rev(rev)
        rows = self._conn.execute(
            "SELECT DISTINCT component FROM cat_interval_rev WHERE rev=? "
            "ORDER BY component",
            (rev,),
        ).fetchall()
        return tuple(r[0] for r in rows)

    def _require_rev(self, rev: int | None) -> int:
        cur = self._current_rev()
        if cur is None:
            raise ValidationError("目录尚无任何修订")
        if rev is None:
            return cur
        hit = self._conn.execute(
            "SELECT 1 FROM cat_revision WHERE rev=?", (rev,)
        ).fetchone()
        if hit is None:
            raise ValidationError(f"修订号 {rev} 不存在")
        return rev

    # ------------------------------------------------------------ 质量产品
    def product(
        self,
        usage: Mapping[str, int],
        rev: int | None = None,
    ) -> UsageProduct:
        """对给定用量组合独立计算质量边界产品。

        对每个成分 c::

            质量和下界 = Σ grams_i * lo_i,c
            质量和上界 = Σ grams_i * hi_i,c
            分数界     = 质量界 / total_g

        未知 lot、负用量或零总量都按 :class:`ValidationError` 拒绝。
        """
        rev = self._require_rev(rev)
        lots = {lot.lot_id: lot for lot in self.get_lots(rev)}
        clean: dict[str, int] = {}
        for lot_id, grams in usage.items():
            if lot_id not in lots:
                raise ValidationError(f"用量中出现未知 lot: {lot_id!r}")
            if not isinstance(grams, int) or grams < 0:
                raise ValidationError(f"lot {lot_id} 用量必须是非负整数")
            if grams:
                clean[lot_id] = grams

        total = sum(clean.values())
        if total <= 0:
            raise ValidationError("用量合计必须为正整数")

        comps = self.components(rev)
        mass_bounds: dict[str, tuple[Fraction, Fraction]] = {}
        frac_bounds: dict[str, tuple[Fraction, Fraction]] = {}
        for comp in comps:
            m_lo = Fraction(0)
            m_hi = Fraction(0)
            for lot_id, grams in clean.items():
                lo, hi = lots[lot_id].intervals[comp]
                m_lo += grams * lo
                m_hi += grams * hi
            mass_bounds[comp] = (m_lo, m_hi)
            frac_bounds[comp] = (m_lo / total, m_hi / total)

        return UsageProduct(
            usage=dict(clean),
            total_g=total,
            mass_bounds=mass_bounds,
            fraction_bounds=frac_bounds,
            source=f"catalog@rev{rev}",
            components=frozenset(comps),
        )

    def robust_feasible(
        self,
        usage: Mapping[str, int],
        constraints: Sequence[ComponentConstraint],
        rev: int | None = None,
    ) -> bool:
        """便捷入口：用量组合对约束是否稳健合格。"""
        return self.product(usage, rev).is_robust_feasible(constraints)
