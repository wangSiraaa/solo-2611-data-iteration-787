# 受限混配方案与实际领料质量核账

纯 Python 3.11 标准库实现（SQLite 持久化 + `fractions.Fraction` 精确有理数 +
自研精确分支定界整数搜索 + 运输多面体最小费用最大流推断），无第三方依赖。
所有数据均为合成，不对应任何真实食品或药品标准。

## 目录结构

```
blend/
  model.py          不可变数据模型、精确有理数、统一异常
  catalog.py        独立原料质量目录（SQLite，原子修订，质量边界产品）
  planner.py        整数混配规划器（精确分支定界）+ 独立全枚举核对器
  warehouse.py      履约仓（预览/确认事务锁、领料/退回/取消状态机、台账、领退事实只读视图）
  classification.py 版本化业务分类（订单→使用部门、lot→物料族，原子修订）
  slice.py          冻结统计切面（净领克数矩阵、贡献者计数、结构性零、范围护栏）
  disclosure.py     定向披露端（精确整数推断、客户历史、价值最大化与字典序平局）
tests/              62 个 unittest 用例（含随机实例对拍与全枚举一致性核对）
demo.py             九个端到端场景演示
```

## 运行

```bash
python3 -m unittest discover -s tests -v   # 全量测试
python3 demo.py                             # 端到端演示（场景 9 为保密发布）
```

## 模块入口

### 1. 质量目录 `blend.catalog.QualityCatalog`

- `replace_lots(lots, note)` —— 一次整体修订（区间与可用量同事务生效），
  失败整体回滚、修订号不动；历史修订永久保留，可按 rev 读取。
- `product(usage, rev=None)` —— 独立计算任意用量组合的每成分质量和上下界
  与分数上下界，产出 `UsageProduct`（不感知订单、不处理优先级）。
- `get_lots / get_lot / components / revisions` —— 修订与 lot 查询。
- 区间语义：同批每克的同一未知成分分数在 `[lo,hi]` 内任取，成分间不声明
  相关性；质量和界为各 lot 贡献区间的线性求和（独立区间算术）。

### 2. 规划器 `blend.planner.Planner`

```python
Planner(order, lots).solve()          # 无解抛 InfeasibleError
Planner(order, lots).solve_or_none()  # 无解返回 None
brute_force_best(order, lots)         # 独立第二条实现：笛卡尔积全枚举
```

- 决策变量本身就是整数克数（0 或步长倍数，截断到库存/M），**不做连续比例
  再舍入**；
- 稳健质量语义：所有 Fraction 端点统一放大成整数，稳健合格落地为整系数
  线性不等式 `Σu·lo ≥ L·M` 且 `Σu·hi ≤ H·M`，对目录允许的全部取值恒成立；
- 三级字典序目标：成本最小 → 正用量 lot 数最少 → 排序 lot_id 用量向量
  字典序最小（元组比较天然保证）；
- 剪枝：子集容量/步长 gcd/质量包络预筛 + DFS 后缀容量、gcd、质量可达包络、
  线性成本下界剪枝；规模上限（12 批、M≤300、步长 1..20、4 成分）下秒级完成。
- `evaluate_usage(lots, usage)` 不依赖目录即可算质量产品，仓库可消费外部产品。

### 3. 履约仓 `blend.warehouse.Warehouse`

| 入口 | 语义 |
| --- | --- |
| `register_lots` | 登记物料档案与初始库存 |
| `plan_order / preview_order` | 规划并保存预览，绑定目录 rev，**不占料** |
| `preview_external` | 消费外部 `PlanResult/UsageProduct`，仓库不重新解释质量 |
| `confirm_preview` | 单 `BEGIN IMMEDIATE` 事务：版本核对 → 库存核对 → 占料落账 |
| `issue` | 实际领料（PENDING → 已领），写 ISSUE 台账 |
| `return_material` | 已领份额登记退回入库，ISSUE/RETURN 双留痕 |
| `cancel_order` | 只释放尚未领出的份额；已领部分不复活 |
| `get_order / explain_order / inventory` | 查询与解释入口 |

- **并发争料**：确认在写锁事务内逐 lot 复核 `initial - allocated - issued
  + returned`，两个独立连接不可能双花同一份库存，失败方收到
  `ConflictError`（或锁等待超时）。
- **质量换代**：确认时若目录 rev 与预览绑定值不一致，事务整体回滚并抛
  `StalePreviewError`，预览标记 `REJECTED_STALE`，绝不把旧方案套新质量；
  已确认订单永久保存确认时的质量边界快照与来源 rev，旧确认结果保持来源。
- **状态机**：`PENDING → PARTIAL_ISSUED → ISSUED`；取消置 `RELEASED`；
  退回置 `PARTIAL_RETURNED/RETURNED`。文件库重开后全部状态完整保持。
- **领退事实只读视图**：`issue_facts(boundary_id)` 只导出真实 ISSUE/RETURN
  （ISSUE 正、RETURN 负），CONFIRM 占料与 CANCEL_RELEASE 未领释放不在其中；
  `max_ledger_id()` 提供切面固定的源事件边界。现有领退与查询 API 全部保持不变。

### 4. 业务分类 `blend.classification.ClassificationStore`

- `replace_classification(order_departments, lot_families, note)` —— 整体
  原子修订（订单→使用部门、lot→物料族），失败回滚、修订号不动，历史 rev 永久可查；
- `order_departments / lot_families / departments / families` —— 按 rev 读取；
- 切面只绑定分类修订号，不复制归类；分类缺口由切面显式报错，禁止静默删除。

### 5. 冻结统计切面 `blend.slice.SliceOffice` / `build_slice`

| 入口 | 语义 |
| --- | --- |
| `freeze_from(warehouse, classification, k=, structural_zeros=, ...)` | 固定事件边界+分类 rev+门槛 k，冻结净领克数矩阵，返回 slice_id |
| `get_slice(slice_id)` | 内网精确矩阵、贡献者计数、主保护格清单（统计端） |
| `get_order_contributions(slice_id)` | 逐订单逐格冻结净量，贡献者计数可独立重算 |
| `public_view(slice_id)` | 对外固定公开：行列合计、维表、结构性零（无格内克数/人数） |

聚合口径（纯函数 `build_slice` 可脱离 SQLite 单测）：

- 每订单在每格对边界内 ISSUE 取正、RETURN 取负求**非负净量**；未领确认与取消
  释放不计；全部退回净量归零，该订单退出该格贡献者计数；
- 每格累计净量，贡献者数 = 净量为正的**不同订单数**（同订单分两次领只算一家）；
- **主保护格** = 非空且正贡献者少于 k；普通零格不是公开的结构性零；
- 结构性不可能组合由显式清单给出，有正净量落入即 `StructuralZeroViolation`
  整体失败；分类不全抛 `ClassificationGapError` 并列出待分派对象；
- 研究范围护栏：每表 ≤ 4×4、每个行列合计 ≤ 200；
- 冻结结果整体落 SQLite，新领退/退料/分类修订都不改变已冻结矩阵（重开保持）。

### 6. 定向披露 `blend.disclosure.DisclosureOffice`

威胁模型（题面限定）：读者只掌握固定公开的行列合计/分类/结构性零，加上自己
在同一切面**实际获准**的精确值；不抵御额外外部知识，不跨切面组合推断。

- `request_disclosure(slice_id, customer, required=[格...], optional=[(格, 正整数价值)...])`
  —— 按客户定向发布；被拒抛 `DisclosureRejected` 且历史不变；
- `check_request(...)` 干跑不落库；`granted_cells / customer_view / request_log` 查询；
- 安全判定：把「客户历史 ∪ 本次拟公开格」代入所有相容**非负整数矩阵**，每个主
  保护格都必须至少有两个可能值；主保护格本身永远不得公开；
- 推断引擎 `MarginNetwork` 利用运输多面体全单位模性，以 SPFA 最小费用最大流
  **精确**求任一格最小/最大可行整数流；`brute_force_cell_values` 是独立全枚举
  第二条实现，回归中逐格对拍（小表）；
- 必选格/合计本身不安全则整次拒绝；否则在安全前提下**最大化新增可选格价值**，
  平局按 (行名, 列名) 取字典序最小清单；已公开格不重复计价值；
- 每次成功披露并入该客户历史（SQLite 持久化，重开保持）；两个单项各自安全、
  合并会泄露的请求，同一客户不能都成功，不同客户互不相干；
- 对外只输出获准值与固定合计，**从不输出支持人数或被盖格原值**。

## 保密推断的正确性依据

带行列合计与部分定边的非负整数矩阵可行域，等价于一个带上下界的二部图整数流
网络（源→部门→物料族→汇）。该约束矩阵全单位模，线性松弛的顶点必为整数解，
因此最小费用最大流给出的边流量上下界就是格值在**全部整数矩阵**上的精确最小/
最大值；当某主保护格的最小可行值等于最大可行值时，它已被公开条件唯一确定
（可反推），判定为不安全。流实现与无图论的递归全枚举 `brute_force_cell_values`
在数百个随机小表上逐格值域一致。

## 演示场景（`demo.py`）

1. 目录独立质量产品与精确分数边界；
2. 中点可行而稳健不行（便宜宽区间料被规划器拒绝）；
3. 固定费改变最优方案；
4. 包装步长约束与步长 gcd 不可达的 INFEASIBLE；
5. 预览不占料 → 确认 → 部分领料 → 取消只放未领 → 退回留痕；
6. 两个独立线程/连接抢最后 10g 库存，恰好一个成功、审计无双花；
7. 质量换代：旧预览拒绝、按新质量重新规划、旧确认保持 rev1 来源；
8. 12 批/M=300/4 成分最大规模性能，以及小库存分支定界 vs 全枚举最优性核对；
9. 稀疏领料交叉表：部分退/全额退的贡献者计数 → 分类缺口与结构性零拦截 →
   冻结切面 → 定向披露（含点名保护格拒绝、历史不变、独立客户）→ 关库重开后
   冻结矩阵与披露历史保持、新领退不改旧切面。

## 关键设计取舍

- **精确算术优先**：端点用 `Fraction`，搜索前按所有分母 LCM 放大为整数，
  全链路无浮点；压线可行/不可行判定可复现。
- **规划与目录解耦**：规划器只吃 `Lot` 列表，`evaluate_usage` 可对任意外部
  lot 集出产品；仓库通过 `preview_external` 消费外部质量产品。
- **SQLite 即锁即核**：不另建锁服务，`BEGIN IMMEDIATE` 拿库级写锁后在事务内
  读最新库存行复核，配合 `busy_timeout` 串行化确认。
- **台账不可变**：领料与退回各立 ledger 行，库存用
  `initial - allocated - issued + returned` 守恒口径推导，取消不抹历史。
- **剪枝只剪不可行支**：后缀质量包络是可达集的外包络，可能保守但绝不会
  剪掉可行解；正确性另由 `brute_force_best` 的 300 例随机对拍兜底。
- **冻结即快照**：切面只认边界前的 ISSUE/RETURN 事实与绑定分类 rev，冻结后
  任何新领退或改分类都落到新切面，旧矩阵逐格不变；普通零格与结构性零严格区分。
- **推断只用公开信息**：保密判定的输入只有固定合计、结构性零与该客户获准值，
  流算法在整数矩阵全集上求保护格值域，不依赖贡献者人数等内部信息；对外视图
  物理上不含人数列与被盖格原值。
- **双实现对拍**：披露引擎的最小费用流与递归全枚举在小表上逐格值域一致，
  大表（4×4、合计 200）只用毫秒级流算法，全枚举仅作小例独立核对器。
