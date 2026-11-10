# archive/agents_delivery_placeholders_20261109 —— 外部交付落点的占位说明存档

> **本目录不进主线**：它不在任何 Python 包路径上、不进 wheel、不进镜像、不被任何门禁扫描。
> 作用只有一个：**保留"并入之前这里是什么样子"的原貌**，让"外部智能体交付落点"这个目录
> 从占位变成真实实现这件事有据可查。

## 存档缘由（2026-11-09）

仓库根的 `agents/` 目录是架构文档规定的 **L3 五大智能体交付落点**（见 `docs/architecture.md`
的 L3 行与 `docs/agents/README.md` 的定位说明）。并入外部智能体模块之前，该目录下五个子目录
（`perceive/` `assess/` `plan/` `execute/` `feedback/`）**各只有一份 README 占位**，
状态一律是「未交付」。

2026-11-09 把同学的外部实现并入后：

- 主线实现落在 **`backend/src/aegis/agents_ext/`**（在 `aegis` 包内，随 wheel 与镜像发布；
  仓库根 `agents/` 不在包路径上，放那里会导致部署时镜像里没有这些代码）；
- 仓库根 `agents/` 保留为**交付落点的索引**（状态表更新为「已交付」并指向实现位置）。

因此这五份占位 README 与根 README 的原貌被**原样存档在这里**——不删、不改写历史，
主线上的对应文件是"现在的样子"，这里是"并入之前的样子"。

## 本目录内容

| 文件 | 原位置 | 说明 |
| --- | --- | --- |
| `README_root.md` | `docs/agents/README.md` | 交付落点的总说明（并入前的版本，状态表全为「未交付」） |
| `perceive/README.md` | `docs/agents/perceive/README.md` | 感知智能体的接入占位说明 |
| `assess/README.md` | `docs/agents/assess/README.md` | 研判智能体的接入占位说明 |
| `plan/README.md` | `docs/agents/plan/README.md` | 决策智能体的接入占位说明 |
| `execute/README.md` | `docs/agents/execute/README.md` | 执行智能体的接入占位说明 |
| `feedback/README.md` | `docs/agents/feedback/README.md` | 反馈智能体的接入占位说明 |

## 一条必须记住的分工（并存，不是替换）

平台侧自建的内环（`backend/src/aegis/agents/` 的 perceive/assess/plan/execute/feedback）
**没有被删除，也没有被替换**——它们仍在生产链路上服役，且已通过契约一致性门禁
（`backend/tests/contract/` 的 7 项）。外部实现是**并存的第二套实现**，
由运行配置选择走哪一套（见 `backend/src/aegis/agents_ext/` 的适配层与装配开关）。
