# ADR-0006：预警触发规则的版本化与标定闭环

- 状态：已采纳
- 日期：2026-10-03
- 相关：`services/trigger_rules.py`、`services/risk_engine.py`、`persistence/sql/005_trigger_rules.sql`、
  `persistence/rulebook.py`、`services/calibration.py`、`api/app.py`（`/api/v1/rules*`）、对应批次见《课题6_完善计划》C2/C3

## 背景

5 类灾种的触发阈值此前只存在于 `default_rulebook()` 的代码里，而它自己的注释就写着
"阈值量级取自公开规范，可由智能体方/专家评审替换"。这意味着三件事同时不成立：

1. **说不出用的是哪一版**：现场改一次阈值就要改代码、过一遍门禁、重新发版，改完也没有痕迹；
2. **回滚不动**：判级偏差要退回上一版时，唯一的办法是回退整个镜像；
3. **标定没有闭环**：准确率回放（`persistence/replay.py`）能算出"报得对不对"，
   但算不到"哪条规则误报多"，于是没有任何输入可以支撑专家评审。

同时必须守住架构铁律 4：阈值口径只能有一份。把规则搬进库，最坏的结果不是搬不动，
而是"库里的值"和"代码里的种子"各说各话。

## 决策

**规则进库，但判据算术留在内核。**

| 面 | 落点 | 口径 |
| --- | --- | --- |
| 存储 | 表 `trigger_rules`，主键 `(rule_id, version)` | 三态 `draft/active/retired`；等级 1–5、`weight∈(0,10]`、`mode∈{all,any}`、`conditions` 必须是 JSON 数组，全部写成库内 CHECK |
| 唯一生效 | 部分唯一索引 `WHERE status='active'` | 同一 `rule_id` 不允许两条 active：两条同时生效就是两套阈值一起判，而报表只显示一个等级 |
| 种子 | 迁移 `005_trigger_rules.sql` 的 9 行 `INSERT ... ON CONFLICT DO NOTHING` | 与 `default_rulebook()` 逐字段一致，由 `tests/unit/test_trigger_rules_library.py` 解析 SQL 文本比对钉住 |
| 装载 | 启动期 `PlatformContainer._load_rulebook()`（唯一读库点） | 存储后端不提供规则库、读失败、或读回来无可装载行 → **沿用内置种子并在 `rulebook_error` 留一行事实**，绝不清空判据 |
| 换版 | `RuleEngine.reload()`，`RiskEngine` 经 `on_reload` 回调同步等级/权重映射 | 空集、同 id 多版本、混入非 active 一律拒绝装载；换版后 `/api/v1/rules` 与 `/metrics/latency` 的 `rulebook` 同一快照 |
| 标定 | `services/calibration.py`（纯算式）+ `GET /api/v1/rules/calibration` | 命中次数取自链路台账（实测）；误报精度必须有现场真值，缺就 `status=not_measured`、`precision=null`；**建议恒 `applied=false`**，生效只能由人工把新版本写回库 |
| 退役 | 退役只改 `status`，不删行 | 回放要能按"当时生效的那一版"复算，删行等于改掉历史 |

`/api/v1/rules/versions`（版本历史）只在 PostgreSQL 形态存在，内存读视图一律 503 并写明
`requires`——内存侧没有"版本化阈值"这件事，把当前生效集伪装成版本清单就是第二套口径。

## 替代方案

- **只留代码，不建表**：省一张表，代价是"改阈值 = 改代码 = 重新发版"，且标定报表永远没有可改的对象。否决。
- **阈值进库并让内核直接查库**：违反架构铁律 3（内核不依赖可选腿的 I/O），
  并且 PG 抖动会直接打断预警判据，而不是留下一行降级事实。否决。
- **在 API 层再算一遍分规则精度**：与 `persistence/replay.py` 的"判定单点"纪律冲突
  （HTTP 层重算算术已经出过一次两口径事故，见 `docs/architecture.md` 的判定单点条目）。否决。

## 后果

- 阈值可追溯：`/api/v1/rules` 一次性给出条数、灾种覆盖、触发条件种类数、版本出处
  （`builtin` / `postgres`）、标定依据与被拒行清单；未标定的规则带着"未经现场标定"出门，
  报表与交底书都不可能把它说成专家结论。
- 新增一条一致性债务：SQL 种子与代码种子是两份文本，靠门禁而不是靠人记得；
  改任一边都必须同时改另一边，否则 `test_代码种子与库种子逐字段一致` 直接红。
- 标定闭环的"最后一公里"仍在数据侧：现场标注案例集（批次 E1）到位之前，
  分规则精度是 `not_measured`，这条不由代码解决。
- 换版生效路径只有"人工审核 + 写库 + 重启或重新装载"，没有热改口子：
  预警判据的变化必须是一次可审计的动作，而不是某个界面按钮的副作用。
