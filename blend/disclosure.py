"""面向实际收件方的单元格定向披露与历史合并（精确整数推断控制）。

威胁模型（题面限定）：

* 读者掌握本切面**固定公开**的行/列合计、分类维表与结构性零；
* 读者知道自己此前在同一切面**实际获准**的全部精确单元格值；
* 不抵御具有额外外部知识的读者，也不处理不同切面之间的组合推断。

每次请求把「既有获准值 ∪ 本次拟公开格」代入所有与公开条件相容的
**非负整数矩阵**；安全当且仅当每个主保护格在可行矩阵集合中至少有两个
不同的可能值。主保护格本身永远不得公开；必选格或合计本身已不安全时
整次拒绝，历史不变。

推断引擎利用运输多面体（二部图流多面体）的全单位模性：带行列合计与
部分定边的非负整数矩阵可行域，恰好是一个带下界的整数流网络，用
SPFA 最小费用最大流即可**精确**求任一边的最小/最大可行流量（整数）。
另提供 :func:`brute_force_cell_values` 全枚举核对器供回归对拍。
"""

from __future__ import annotations

import sqlite3
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .model import DisclosureRejected, ValidationError
from .slice import FrozenSlice, SliceOffice

__all__ = [
    "GridInfo",
    "MarginNetwork",
    "brute_force_cell_values",
    "cell_feasible_range",
    "grid_is_feasible",
    "is_safe",
    "evaluate_request",
    "DisclosureOffice",
    "DisclosureOutcome",
]


# ============================================================ 脱敏网格描述


@dataclass(frozen=True)
class GridInfo:
    """推断引擎所需的公开/冻结网格信息（不含贡献者以外的内部订单明细）。"""

    slice_id: int
    rows: tuple[str, ...]
    cols: tuple[str, ...]
    row_totals: Mapping[str, int]
    col_totals: Mapping[str, int]
    structural_zeros: frozenset[tuple[str, str]]
    protected: frozenset[tuple[str, str]]

    @classmethod
    def from_slice(cls, sl: FrozenSlice) -> "GridInfo":
        return cls(
            slice_id=sl.slice_id,
            rows=sl.rows,
            cols=sl.cols,
            row_totals=dict(sl.row_totals),
            col_totals=dict(sl.col_totals),
            structural_zeros=sl.structural_zeros,
            protected=sl.protected,
        )


# ============================================================ 最小费用最大流


class MarginNetwork:
    """固定行列合计下的非负整数矩阵运输网络（含定边与结构性零）。

    网络::

        S -> 行节点(容量=该行剩余合计) -> 列节点(容量=该格剩余上界)
          -> T(容量=该列剩余合计)

    定边（结构性零或已公开精确值）从自由边中移除，其值先从两侧合计中
    扣除。全单位模保证 max-flow / min-cost-flow 的最优流都是整数，因此
    流值就是可行整数矩阵的格值。
    """

    def __init__(self, grid: GridInfo, fixed: Mapping[tuple[str, str], int]) -> None:
        self.grid = grid
        self.fixed = dict(fixed)
        n_rows, n_cols = len(grid.rows), len(grid.cols)
        self.n_rows, self.n_cols = n_rows, n_cols
        # 节点编号：0=S，1..n_rows=行，n_rows+1..n_rows+n_cols=列，末位=T
        self.S = 0
        self.T = n_rows + n_cols + 1
        self.n_nodes = self.T + 1
        self.row_node = {r: 1 + i for i, r in enumerate(grid.rows)}
        self.col_node = {c: 1 + n_rows + j for j, c in enumerate(grid.cols)}

        rem_row = {r: grid.row_totals[r] for r in grid.rows}
        rem_col = {c: grid.col_totals[c] for c in grid.cols}
        self._cell_edges: dict[tuple[str, str], int] = {}
        # 边: [to, rev_index, cap, cost]
        self._edges: list[list] = []
        for _ in range(self.n_nodes):
            self._edges.append([])

        for cell, value in self.fixed.items():
            r, c = cell
            if cell in grid.structural_zeros:
                if value != 0:
                    raise ValidationError(f"结构性零格 {cell} 被固定为非零值 {value}")
                continue
            if not isinstance(value, int) or value < 0:
                raise ValidationError(f"定边 {cell} 的值必须是非负整数")
            rem_row[r] -= value
            rem_col[c] -= value
        if any(v < 0 for v in rem_row.values()) or any(v < 0 for v in rem_col.values()):
            # 定边之和超过某条合计：可行域为空
            self.feasible = False
            self.required_flow = 0
            return
        self.rem_row, self.rem_col = rem_row, rem_col
        total_row = sum(rem_row.values())
        total_col = sum(rem_col.values())
        if total_row != total_col:
            self.feasible = False
            self.required_flow = 0
            return
        self.required_flow = total_row
        self.feasible = True

        for r in grid.rows:
            self._add_edge(self.S, self.row_node[r], rem_row[r], 0)
        for c in grid.cols:
            self._add_edge(self.col_node[c], self.T, rem_col[c], 0)
        for r in grid.rows:
            for c in grid.cols:
                cell = (r, c)
                if cell in self.fixed or cell in grid.structural_zeros:
                    continue  # 定边与结构性零不建自由边
                # 自由边上界不超过两条余量合计（min 更紧，减少残量）
                cap = min(rem_row[r], rem_col[c])
                idx = self._add_edge(self.row_node[r], self.col_node[c], cap, 0)
                self._cell_edges[cell] = idx

    # ------------------------------------------------------------ 图操作
    def _add_edge(self, u: int, v: int, cap: int, cost: int) -> int:
        idx = len(self._edges[u])
        # 正向边记录在 u 的 idx 位，反向边记录在 v 的对应位
        self._edges[u].append([v, len(self._edges[v]), cap, cost])
        self._edges[v].append([u, idx, 0, -cost])
        return idx

    def _clone_edges(self) -> list[list]:
        return [list(map(list, adj)) for adj in self._edges]

    def _min_cost_flow(
        self,
        edges: list[list],
        target_cell: tuple[str, str] | None,
        maximize: bool,
    ) -> tuple[int, int] | None:
        """在残量图上跑 SPFA 最小费用流，返回 (目标边最终流量, 总费用)。

        不可行（达不到 required_flow）返回 None。给目标格的边赋单位费用
        （最小化取 +1、最大化取 -1，反向边同步取反），最小费用流即把
        目标边流量压到最小/抬到最大；其余边费用为 0。SPFA 正确处理反向
        边的负费用。
        """
        target_idx = None if target_cell is None else self._cell_edges.get(target_cell)
        if target_cell is not None and target_idx is None:
            # 目标边不存在（定边/结构性零）：其流量已被固定，直接读固定值
            if not self.feasible:
                return None
            return (self.fixed.get(target_cell, 0), 0)
        if target_idx is not None:
            u = self.row_node[target_cell[0]]
            w = -1 if maximize else 1
            edges[u][target_idx][3] = w
            rev = edges[u][target_idx][1]
            v = edges[u][target_idx][0]
            edges[v][rev][3] = -w

        flow = 0
        cost_sum = 0
        n = self.n_nodes
        INF = 10**18
        while flow < self.required_flow:
            dist = [INF] * n
            in_queue = [False] * n
            prev_v = [-1] * n
            prev_e = [-1] * n
            dist[self.S] = 0
            queue = deque([self.S])
            in_queue[self.S] = True
            while queue:
                u = queue.popleft()
                in_queue[u] = False
                for ei, e in enumerate(edges[u]):
                    v, rev, cap, w = e
                    if cap <= 0:
                        continue
                    if dist[v] > dist[u] + w:
                        dist[v] = dist[u] + w
                        prev_v[v], prev_e[v] = u, ei
                        if not in_queue[v]:
                            queue.append(v)
                            in_queue[v] = True
            if dist[self.T] == INF:
                return None  # 增广路耗尽，可行域为空
            # 瓶颈增量
            add = self.required_flow - flow
            v = self.T
            while v != self.S:
                u, ei = prev_v[v], prev_e[v]
                add = min(add, edges[u][ei][2])
                v = u
            v = self.T
            while v != self.S:
                u, ei = prev_v[v], prev_e[v]
                e = edges[u][ei]
                e[2] -= add
                edges[v][e[1]][2] += add
                cost_sum += add * e[3]
                v = u
            flow += add

        if target_idx is None:
            return (0, cost_sum)
        # 正向边初始容量 - 当前残量 = 该边最终流量
        u = self.row_node[target_cell[0]]
        forward = edges[u][target_idx]
        initial_cap = forward[2] + edges[forward[0]][forward[1]][2]
        return (initial_cap - forward[2], cost_sum)

    def _run(self, target_cell=None, maximize=False) -> tuple[int, int] | None:
        if not self.feasible:
            return None
        return self._min_cost_flow(self._clone_edges(), target_cell, maximize)

    # ------------------------------------------------------------ 公开判定
    def is_feasible(self) -> bool:
        """固定条件下是否还存在至少一个非负整数相容矩阵。"""
        return self._run() is not None

    def cell_range(self, cell: tuple[str, str]) -> tuple[int, int] | None:
        """目标格在所有相容整数矩阵中的 (最小可行值, 最大可行值)。

        定边/结构性零直接返回其固定值；可行域为空返回 None。
        """
        if cell in self.fixed or cell in self.grid.structural_zeros:
            if not self.feasible:
                return None
            return (self.fixed.get(cell, 0), self.fixed.get(cell, 0))
        if cell not in self._cell_edges:
            return None
        lo = self._run(target_cell=cell, maximize=False)
        hi = self._run(target_cell=cell, maximize=True)
        if lo is None or hi is None:
            return None
        return (lo[0], hi[0])


# ============================================================ 可行性/安全性


def grid_is_feasible(grid: GridInfo, fixed: Mapping[tuple[str, str], int]) -> bool:
    """固定若干精确格后，是否仍存在相容的非负整数矩阵。"""
    return MarginNetwork(grid, fixed).is_feasible()


def cell_feasible_range(
    grid: GridInfo,
    fixed: Mapping[tuple[str, str], int],
    cell: tuple[str, str],
) -> tuple[int, int] | None:
    """暴露给核对器/测试的精确格值域。"""
    return MarginNetwork(grid, fixed).cell_range(cell)


def is_safe(
    grid: GridInfo,
    fixed: Mapping[tuple[str, str], int],
) -> bool:
    """核心保密判定：每个主保护格在相容整数矩阵中都至少有两个可能值。

    固定集合包含结构性零与该客户的全部已获准值（含本次拟公开格）。
    任一主保护格的最小可行值等于最大可行值（可行域为空同样视为不安全）
    即判定泄露。
    """
    net = MarginNetwork(grid, fixed)
    if not net.feasible:
        return False
    for cell in grid.protected:
        if cell in fixed:  # 主保护格被点名公开，直接不安全
            return False
        rng = net.cell_range(cell)
        if rng is None:
            return False
        lo, hi = rng
        if lo >= hi:  # 唯一可能值 -> 可被反推
            return False
    return True


# ============================================================ 独立全枚举核对器


def brute_force_cell_values(
    grid: GridInfo,
    fixed: Mapping[tuple[str, str], int],
    target_cell: tuple[str, str],
) -> set[int] | None:
    """独立第二条实现：递归枚举全部相容非负整数矩阵，收集目标格取值集合。

    与 :class:`MarginNetwork` 的流算法完全独立（无图论、无费用），
    仅适合研究范围内的小表。可行域为空返回 None。
    """
    rows, cols = grid.rows, grid.cols
    row_totals = {r: grid.row_totals[r] for r in rows}
    rem_col = {c: grid.col_totals[c] for c in cols}
    # 行预算预先扣除定边/结构性零的固定值（这些格在递归中不再分配自由量）；
    # 列余量同样预扣，递归只枚举自由格，避免任何双重计数。
    free_budget = dict(row_totals)
    locked: dict[tuple[str, str], int] = dict(fixed)
    for zcell in grid.structural_zeros:
        if zcell in locked and locked[zcell] != 0:
            return None
        locked.setdefault(zcell, 0)
    for lcell, v in locked.items():
        if not isinstance(v, int) or v < 0:
            return None
        lr, lc = lcell
        free_budget[lr] -= v
        rem_col[lc] -= v
    if any(v < 0 for v in free_budget.values()) or any(v < 0 for v in rem_col.values()):
        return None
    if sum(free_budget.values()) != sum(rem_col.values()):
        return None

    values: set[int] = set()
    feasible_found = False
    matrix: dict[tuple[str, str], int] = dict(locked)

    def fill_row(ri: int) -> None:
        nonlocal feasible_found
        if ri == len(rows):
            if all(rem_col[c] == 0 for c in cols):
                feasible_found = True
                values.add(matrix.get(target_cell, 0))
            return
        r = rows[ri]

        def fill_col(ci: int, row_budget: int) -> None:
            if ci == len(cols):
                # 整行枚举完毕：自由预算必须恰好分完（最后一格已保证上界）
                if row_budget == 0:
                    fill_row(ri + 1)
                return
            c = cols[ci]
            cur = (r, c)
            if cur in locked:
                # 固定值/结构性零已在预算与列余量中预扣，递归直接跳过
                fill_col(ci + 1, row_budget)
                return
            # 自由格：0 .. min(本行剩余预算, 本列剩余合计)；
            # 本行之后再无自由格时，上界收紧到 row_budget 以保证行合计闭合
            later_free = [
                cols[k] for k in range(ci + 1, len(cols))
                if (r, cols[k]) not in locked
            ]
            hi = min(row_budget, rem_col[c])
            if not later_free:
                hi = min(hi, row_budget)
            for v in range(hi + 1):
                rem_col[c] -= v
                matrix[cur] = v
                fill_col(ci + 1, row_budget - v)
                rem_col[c] += v
            matrix.pop(cur, None)

        fill_col(0, free_budget[r])

    fill_row(0)
    return values if feasible_found else None


# ============================================================ 请求优化（纯函数）


@dataclass(frozen=True)
class DisclosureOutcome:
    """一次成功披露的结构化结果。"""

    slice_id: int
    customer: str
    granted_required: tuple[tuple[str, str], ...]
    granted_optional: tuple[tuple[str, str], ...]
    optional_value: int
    skipped_already_public: tuple[tuple[str, str], ...]
    granted_values: Mapping[tuple[str, str], int]
    public_view: dict


def _cell_sort_key(cell: tuple[str, str]) -> tuple[str, str]:
    return (cell[0], cell[1])


def evaluate_request(
    grid: GridInfo,
    history: Mapping[tuple[str, str], int],
    required,
    optional,
    real_values: Mapping[tuple[str, str], int],
) -> dict:
    """纯函数评估一次定向披露请求，不写任何持久状态。

    Parameters
    ----------
    grid:
        冻结切面的脱敏网格（含主保护格与结构性零清单）。
    history:
        该客户在本切面已获准的 格 -> 精确值 映射。
    required, optional:
        必选格序列；可选格序列的 ``(row, col, 正整数价值)``。
    real_values:
        冻结矩阵真实格值（内网）；推断引擎要用它扣除行列余量，必填。

    返回 ``{"accepted", "reason", "granted_required", "granted_optional",
    "optional_value", "skipped", "granted_values"}``；拒绝时 accepted=False。
    """
    required = [tuple(x) for x in required]
    optional = [(tuple(c), w) for c, w in optional]
    known_cells = {(r, c) for r in grid.rows for c in grid.cols}

    # ---- 输入校验 -------------------------------------------------------
    for cell in required:
        if cell not in known_cells:
            raise ValidationError(f"必选格 {cell} 不在切面维表内")
    seen: set[tuple[str, str]] = set()
    for cell, w in optional:
        if cell not in known_cells:
            raise ValidationError(f"可选格 {cell} 不在切面维表内")
        if not isinstance(w, int) or w <= 0:
            raise ValidationError(f"可选格 {cell} 的价值必须是正整数，收到 {w!r}")
        if cell in seen:
            raise ValidationError(f"可选格 {cell} 重复出现")
        seen.add(cell)
    if len(required) != len(set(required)):
        raise ValidationError("必选格存在重复")

    # ---- 主保护格永远不得公开（必选/可选点名都整次拒绝）-----------------
    named_protected = sorted(
        {cell for cell in required if cell in grid.protected}
        | {cell for cell, _w in optional if cell in grid.protected}
    )
    if named_protected:
        return {
            "accepted": False,
            "reason": "protected_cell_named",
            "detail": f"主保护格不得公开: {named_protected}",
            "granted_required": (),
            "granted_optional": (),
            "optional_value": 0,
            "skipped": (),
            "granted_values": {},
        }

    # ---- 结构性零已是固定公开信息：静默跳过，不进获准集 -----------------
    zeros = grid.structural_zeros
    required = [cell for cell in required if cell not in zeros]
    optional = [(cell, w) for cell, w in optional if cell not in zeros]

    skipped = tuple(sorted((cell for cell in history if cell in set(required)
                            or cell in {c for c, _ in optional}), key=_cell_sort_key))
    # ---- 必选格：已公开的过滤掉；其余在历史条件下必须全部安全 -----------
    base_fixed = dict(history)
    new_required = [cell for cell in required if cell not in base_fixed]
    trial = dict(base_fixed)
    for cell in new_required:
        trial[cell] = real_values[cell]
    if new_required and not is_safe(grid, trial):
        return {
            "accepted": False,
            "reason": "unsafe",
            "detail": "必选格（结合既有历史与固定合计）会使某主保护格可被反推",
            "granted_required": (),
            "granted_optional": (),
            "optional_value": 0,
            "skipped": skipped,
            "granted_values": {},
        }

    # ---- 可选格：安全前提下最大化新增价值，字典序最小平局 ---------------
    # 已公开格不得重复计价值；与必选重合的可选格也视为已覆盖。
    covered = set(base_fixed) | set(new_required)
    candidates = [(cell, w) for cell, w in optional if cell not in covered]
    # 按 (行名, 列名) 排序，DFS 先选字典序小的格，天然产出最小平局清单
    candidates.sort(key=lambda x: _cell_sort_key(x[0]))
    suffix_value = [0] * (len(candidates) + 1)
    for i in range(len(candidates) - 1, -1, -1):
        suffix_value[i] = suffix_value[i + 1] + candidates[i][1]

    best = {"value": -1, "cells": ()}
    failed_cache: set[frozenset] = set()

    def dfs(pos: int, chosen: list[tuple[str, str]], value: int) -> None:
        # 上界剪枝：连理论最大值都超不过当前最优就不必继续
        if value + suffix_value[pos] < best["value"]:
            return
        chosen_set = frozenset(chosen)
        # 已知某 chosen 子集不安全 => 当前超集必不安全（定边只会收紧可行域）
        if any(bad <= chosen_set for bad in failed_cache):
            return
        if pos == len(candidates):
            if chosen_set in failed_cache:
                return
            fixed_now = dict(trial)
            for cell in chosen:
                fixed_now[cell] = real_values[cell]
            if not is_safe(grid, fixed_now):
                failed_cache.add(chosen_set)
                return
            if value > best["value"] or (
                value == best["value"] and tuple(sorted(chosen)) < best["cells"]
            ):
                best["value"] = value
                best["cells"] = tuple(sorted(chosen))
            return
        cell, w = candidates[pos]
        # 先尝试选入（字典序小的格优先被选，平局时清单更小）
        chosen.append(cell)
        dfs(pos + 1, chosen, value + w)
        chosen.pop()
        dfs(pos + 1, chosen, value)

    dfs(0, [], 0)
    chosen_optional = best["cells"]
    granted_values = dict(base_fixed)
    for cell in new_required:
        granted_values[cell] = real_values[cell]
    for cell in chosen_optional:
        granted_values[cell] = real_values[cell]

    return {
        "accepted": True,
        "reason": "",
        "granted_required": tuple(sorted(new_required, key=_cell_sort_key)),
        "granted_optional": chosen_optional,
        "optional_value": best["value"] if best["value"] >= 0 else 0,
        "skipped": skipped,
        "granted_values": granted_values,
    }


# ============================================================ 持久化披露端


_SCHEMA = """
CREATE TABLE IF NOT EXISTS dl_grants (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    slice_id  INTEGER NOT NULL,
    customer  TEXT NOT NULL,
    row_name  TEXT NOT NULL,
    col_name  TEXT NOT NULL,
    grams     INTEGER NOT NULL,
    kind      TEXT NOT NULL,
    req_seq   INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(slice_id, customer, row_name, col_name)
);
CREATE TABLE IF NOT EXISTS dl_request_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    slice_id  INTEGER NOT NULL,
    customer  TEXT NOT NULL,
    accepted  INTEGER NOT NULL,
    reason    TEXT NOT NULL,
    detail    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class DisclosureOffice:
    """按客户维护披露历史的定向发布端（SQLite 持久化，重开历史保持）。

    与 :class:`~blend.slice.SliceOffice` 共用或独立数据库均可；获准值只存
    本端表，冻结矩阵仍以切面库为准。输出端永远不暴露贡献者计数与被盖格
    原值。
    """

    def __init__(
        self,
        slice_office: SliceOffice,
        db_path: str | Path | None = None,
        busy_timeout_ms: int = 10000,
    ) -> None:
        self.slices = slice_office
        self._owns_connection = False
        if db_path is None and slice_office.path == ":memory:":
            # 内存库每个连接是互相隔离的私有库：第二个连接看不到切面表，
            # 因此内存场景必须复用切面库的同一连接（表前缀互不冲突）。
            self.path = ":memory:"
            self._conn = slice_office._conn
        else:
            # 文件库（含与切面库同文件）开独立连接，由 SQLite 写锁串行化，
            # 两个对象的事务与生命周期互不干扰。
            self.path = slice_office.path if db_path is None else str(db_path)
            self._conn = sqlite3.connect(self.path)
            self._conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
            self._owns_connection = True
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._seq_cache: dict[tuple[int, str], int] = {}

    def close(self) -> None:
        # 复用的内存库连接归切面库所有，这里不代为关闭
        if self._owns_connection:
            self._conn.close()

    def __enter__(self) -> "DisclosureOffice":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------ 内部
    def _grid_and_real(self, slice_id: int) -> tuple[GridInfo, FrozenSlice]:
        sl = self.slices.get_slice(slice_id)  # 不存在会抛 ValidationError
        return GridInfo.from_slice(sl), sl

    def _history(self, slice_id: int, customer: str) -> dict[tuple[str, str], int]:
        rows = self._conn.execute(
            "SELECT row_name, col_name, grams FROM dl_grants"
            " WHERE slice_id=? AND customer=?",
            (slice_id, customer),
        ).fetchall()
        return {(r, c): g for r, c, g in rows}

    def _next_seq(self, slice_id: int, customer: str) -> int:
        key = (slice_id, customer)
        if key not in self._seq_cache:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(req_seq), 0) FROM dl_grants"
                " WHERE slice_id=? AND customer=?",
                key,
            ).fetchone()
            self._seq_cache[key] = int(row[0])
        self._seq_cache[key] += 1
        return self._seq_cache[key]

    # ------------------------------------------------------------ 主入口
    def check_request(
        self,
        slice_id: int,
        customer: str,
        required=(),
        optional=(),
    ) -> dict:
        """干跑评估：返回与 :func:`evaluate_request` 同构的结论，不落库。"""
        grid, sl = self._grid_and_real(slice_id)
        history = self._history(slice_id, customer)
        return evaluate_request(
            grid, history, required, optional, real_values=sl.matrix
        )

    def request_disclosure(
        self,
        slice_id: int,
        customer: str,
        required=(),
        optional=(),
    ) -> DisclosureOutcome:
        """提交一次定向发布；被拒抛 :class:`DisclosureRejected` 且历史不变。

        成功时把本次**新增**获准格并入该客户历史（已公开格不重复计价值、
        不重复落库），并返回获准精确值与固定合计的对外视图。
        """
        if not isinstance(customer, str) or not customer:
            raise ValidationError("客户标识必须是非空字符串")
        grid, sl = self._grid_and_real(slice_id)
        history = self._history(slice_id, customer)
        result = evaluate_request(
            grid, history, required, optional, real_values=sl.matrix
        )
        now = datetime.now(timezone.utc).isoformat()
        if not result["accepted"]:
            self._conn.execute(
                "INSERT INTO dl_request_log(slice_id, customer, accepted, reason,"
                " detail, created_at) VALUES (?,?,?,?,?,?)",
                (slice_id, customer, 0, result["reason"], result["detail"], now),
            )
            self._conn.commit()
            raise DisclosureRejected(result["reason"], result["detail"])

        granted_required = set(result["granted_required"])
        new_cells = [(c, result["granted_values"][c])
                     for c in (granted_required | set(result["granted_optional"]))
                     if c not in history]
        new_cells.sort(key=lambda x: _cell_sort_key(x[0]))
        seq = self._next_seq(slice_id, customer)
        conn = self._conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            for cell, g in new_cells:
                kind = "REQUIRED" if cell in granted_required else "OPTIONAL"
                conn.execute(
                    "INSERT INTO dl_grants(slice_id, customer, row_name, col_name,"
                    " grams, kind, req_seq, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (slice_id, customer, cell[0], cell[1], g, kind, seq, now),
                )
            conn.execute(
                "INSERT INTO dl_request_log(slice_id, customer, accepted, reason,"
                " detail, created_at) VALUES (?,?,?,?,?,?)",
                (slice_id, customer, 1, "accepted",
                 f"+{len(new_cells)} cells, optional_value={result['optional_value']}", now),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return self._outcome(slice_id, customer, result, sl)

    # ------------------------------------------------------------ 查询
    def granted_cells(self, slice_id: int, customer: str) -> dict[tuple[str, str], int]:
        """该客户在本切面已获准的精确格值（重开后仍完整）。"""
        self._grid_and_real(slice_id)
        return dict(self._history(slice_id, customer))

    def customer_view(self, slice_id: int, customer: str) -> dict:
        """对外输出：固定合计 + 该客户获准值；不含人数/被盖格原值。"""
        _grid, sl = self._grid_and_real(slice_id)
        history = self._history(slice_id, customer)
        view = sl.public_view()
        view["customer"] = customer
        view["disclosed_cells"] = [
            {"row": r, "col": c, "grams": g}
            for (r, c), g in sorted(history.items(), key=lambda kv: _cell_sort_key(kv[0]))
        ]
        return view

    def request_log(self, slice_id: int | None = None) -> list[dict]:
        """审计用：全部请求（含被拒），按时间顺序。"""
        if slice_id is None:
            rows = self._conn.execute(
                "SELECT slice_id, customer, accepted, reason, detail, created_at"
                " FROM dl_request_log ORDER BY id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT slice_id, customer, accepted, reason, detail, created_at"
                " FROM dl_request_log WHERE slice_id=? ORDER BY id",
                (slice_id,),
            ).fetchall()
        return [
            {"slice_id": r[0], "customer": r[1], "accepted": bool(r[2]),
             "reason": r[3], "detail": r[4], "created_at": r[5]}
            for r in rows
        ]

    def _outcome(self, slice_id, customer, result, sl) -> DisclosureOutcome:
        new_history = self._history(slice_id, customer)
        return DisclosureOutcome(
            slice_id=slice_id,
            customer=customer,
            granted_required=result["granted_required"],
            granted_optional=result["granted_optional"],
            optional_value=result["optional_value"],
            skipped_already_public=result["skipped"],
            granted_values=new_history,
            public_view=self.customer_view(slice_id, customer),
        )
