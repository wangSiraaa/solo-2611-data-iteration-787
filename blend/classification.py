"""版本化的订单/lot 业务分类（SQLite 持久化，原子修订）。

冻结「使用部门 × 物料族」切面需要两套与履约事实解耦的业务归类：

* 订单 ``order_id`` -> 使用部门 ``department``；
* 原料批 ``lot_id`` -> 物料族 ``family``。

分类像质量目录一样整体修订、永久保留历史修订：切面冻结时绑定具体
``class_rev``，之后即便改分类，已冻结矩阵不变。分类只表达归类关系，
不复制任何克数；未覆盖的对象由切面模块显式报错（
:class:`~blend.model.ClassificationGapError`），绝不静默删除。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .model import ValidationError

__all__ = ["ClassificationStore"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cls_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cls_revision (
    rev        INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    note       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cls_order_rev (
    rev        INTEGER NOT NULL,
    order_id   TEXT NOT NULL,
    department TEXT NOT NULL,
    PRIMARY KEY (rev, order_id)
);
CREATE TABLE IF NOT EXISTS cls_lot_rev (
    rev      INTEGER NOT NULL,
    lot_id   TEXT NOT NULL,
    family   TEXT NOT NULL,
    PRIMARY KEY (rev, lot_id)
);
"""


class ClassificationStore:
    """SQLite 支持的版本化业务分类。

    Parameters
    ----------
    path:
        数据库文件路径；``":memory:"`` 为内存库。文件库重开后修订完整保持。
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

    def __enter__(self) -> "ClassificationStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------ 内部工具
    def _current_rev(self) -> int | None:
        row = self._conn.execute(
            "SELECT value FROM cls_meta WHERE key = 'current_rev'"
        ).fetchone()
        return None if row is None else int(row[0])

    @property
    def revision(self) -> int | None:
        """当前分类修订号；尚无任何修订时为 ``None``。"""
        return self._current_rev()

    def revisions(self) -> list[tuple[int, str, str]]:
        """全部历史修订 ``(rev, created_at, note)``，按修订号升序。"""
        return list(
            self._conn.execute(
                "SELECT rev, created_at, note FROM cls_revision ORDER BY rev"
            ).fetchall()
        )

    def _require_rev(self, rev: int | None) -> int:
        cur = self._current_rev()
        if cur is None:
            raise ValidationError("分类库尚无任何修订")
        if rev is None:
            return cur
        hit = self._conn.execute(
            "SELECT 1 FROM cls_revision WHERE rev=?", (rev,)
        ).fetchone()
        if hit is None:
            raise ValidationError(f"分类修订号 {rev} 不存在")
        return rev

    # ------------------------------------------------------------ 修订
    def replace_classification(
        self,
        order_departments: dict[str, str],
        lot_families: dict[str, str],
        note: str = "",
    ) -> int:
        """整体修订：用两张完整归类表替换当前分类，返回新修订号。

        校验与写入在单事务内完成；空键、空归类名、重复键都整体回滚，
        修订号不动。修订是整体快照而非增量补丁，保证每个 rev 自洽。
        """
        od = dict(order_departments)
        lf = dict(lot_families)
        for mapping, what in ((od, "订单归类"), (lf, "lot 归类")):
            if not isinstance(mapping, dict):
                raise ValidationError(f"{what}必须是 dict 映射")
            for key, val in mapping.items():
                if not isinstance(key, str) or not key:
                    raise ValidationError(f"{what}的键必须是非空字符串")
                if not isinstance(val, str) or not val:
                    raise ValidationError(f"{what}中 {key!r} 的归类必须是非空字符串")
        if len(od) != len(set(od)) or len(lf) != len(set(lf)):  # dict 本身无重复键，防御
            raise ValidationError("分类映射存在重复键")

        prev_rev = self._current_rev()
        new_rev = 1 if prev_rev is None else prev_rev + 1
        now = datetime.now(timezone.utc).isoformat()
        try:
            self._conn.execute("BEGIN")
            self._conn.execute(
                "INSERT INTO cls_revision(rev, created_at, note) VALUES (?,?,?)",
                (new_rev, now, note),
            )
            self._conn.executemany(
                "INSERT INTO cls_order_rev(rev, order_id, department) VALUES (?,?,?)",
                [(new_rev, o, d) for o, d in sorted(od.items())],
            )
            self._conn.executemany(
                "INSERT INTO cls_lot_rev(rev, lot_id, family) VALUES (?,?,?)",
                [(new_rev, lot, fam) for lot, fam in sorted(lf.items())],
            )
            self._conn.execute(
                "INSERT INTO cls_meta(key, value) VALUES('current_rev', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(new_rev),)
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return new_rev

    # ------------------------------------------------------------ 查询
    def order_departments(self, rev: int | None = None) -> dict[str, str]:
        """取某修订的全部 订单->部门 映射（拷贝，调用方可自由修改）。"""
        rev = self._require_rev(rev)
        rows = self._conn.execute(
            "SELECT order_id, department FROM cls_order_rev WHERE rev=?", (rev,)
        ).fetchall()
        return {o: d for o, d in rows}

    def lot_families(self, rev: int | None = None) -> dict[str, str]:
        """取某修订的全部 lot->物料族 映射（拷贝，调用方可自由修改）。"""
        rev = self._require_rev(rev)
        rows = self._conn.execute(
            "SELECT lot_id, family FROM cls_lot_rev WHERE rev=?", (rev,)
        ).fetchall()
        return {lot: fam for lot, fam in rows}

    def departments(self, rev: int | None = None) -> tuple[str, ...]:
        """该修订出现过的全部部门，按名称排序（矩阵行名口径）。"""
        rev = self._require_rev(rev)
        rows = self._conn.execute(
            "SELECT DISTINCT department FROM cls_order_rev WHERE rev=? ORDER BY department",
            (rev,),
        ).fetchall()
        return tuple(r[0] for r in rows)

    def families(self, rev: int | None = None) -> tuple[str, ...]:
        """该修订出现过的全部物料族，按名称排序（矩阵列名口径）。"""
        rev = self._require_rev(rev)
        rows = self._conn.execute(
            "SELECT DISTINCT family FROM cls_lot_rev WHERE rev=? ORDER BY family",
            (rev,),
        ).fetchall()
        return tuple(r[0] for r in rows)

    def department_of(self, order_id: str, rev: int | None = None) -> str | None:
        rev = self._require_rev(rev)
        row = self._conn.execute(
            "SELECT department FROM cls_order_rev WHERE rev=? AND order_id=?",
            (rev, order_id),
        ).fetchone()
        return None if row is None else row[0]

    def family_of(self, lot_id: str, rev: int | None = None) -> str | None:
        rev = self._require_rev(rev)
        row = self._conn.execute(
            "SELECT family FROM cls_lot_rev WHERE rev=? AND lot_id=?",
            (rev, lot_id),
        ).fetchone()
        return None if row is None else row[0]
