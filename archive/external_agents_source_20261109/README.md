# archive/external_agents_source_20261109 —— 外部智能体模块原始物料存档

> **本目录不进主线**：不在 `backend/src/` 里、不进 wheel、不进镜像、不被 mypy/ruff/pytest 扫描。
> 它的唯一作用是**溯源与复核**——"那份并进来的代码，原仓库里还剩些什么没并进来、为什么"。
> 改代码请改 `backend/src/aegis/agents_ext/`（主线实现），**不要**在这里改。
> **`scripts/` 子目录仅保留原始交付快照，不再作为更新入口**——外部模块已转为在树维护，
> 不要再用这些搬入/后处理脚本覆盖主线实现（2026-11-09 收口，见 `docs/adr/0012`）。

## 来源

- 仓库：`https://github.com/aaaaaelma-alt/hazard-landslide-agent`（同学的智能体模块，独立 LangGraph 多智能体实现）
- 并入时间：2026-11-09（分支 `feat/agents-ext-merge`）
- 并入方式：代码迁入 `backend/src/aegis/agents_ext/`（包内、随镜像发布）；
  本目录只留**不适合进主线**的原始物料。

## 本目录里的东西与它们不进主线的原因

| 文件 | 是什么 | 为什么只存档 |
| --- | --- | --- |
| `5类高原典型山地灾害.zip` | 18MB 灾害数据集 | **全仓零代码引用**（grep `.zip` 在 `.py` 里 0 命中）。体积大、二进制、来源与授权未核，不进主线 |
| `5类灾害触发条件.docx` | 触发条件规范（同学写的） | 同上：文档不进代码仓；平台侧对应物是 `backend/src/aegis/services/rules/`（规则库） |
| `搜索提示.docx` / `西藏灾害多智能体.docx` | 设计说明 | 同上 |
| `test_fb_result.txt` | 一次运行输出 | 运行产物，不进主线 |
| `runtime_artifacts/manager_checkpoints.sqlite` | Manager 的 SQLite checkpoint | **运行时产物**。主线里 checkpoint 路径由配置给（`AEGIS_EXT_CHECKPOINT_DB`），不进仓 |
| `runtime_artifacts/feedback_memory.json` | 反馈记忆的 JSON 存储 | 同上：主线里是可配置路径的运行时文件，且**会被测试写**（写进仓会每次跑测试都产生 diff） |
| `config_original_WITH_SECRET.py` | 原仓库的配置面 | ⚠️ **原文件里是明文 API Key，已在本副本中抹除**（`ZHIPUAI_API_KEY = ""`）。主线版本见 `backend/src/aegis/agents_ext/config.py`，只读环境变量 |
| `scripts/import_external_agents*.py` / `postprocess_external_agents*.py` / `import_external_agents_all.sh` | 并入时的搬入/测试迁移/后处理/类型修正脚本 | **已退役**：模块现在树维护，这些脚本再跑会覆盖主线实现（且搬入时曾把密钥带回来过一次）。只作原始交付的复原快照 |

## 两件必须记下来的事

1. **原仓库曾提交明文 API Key**（智谱开放平台）。该 Key 已随本仓库公开，**必须在智谱侧轮换**；
   主线实现不读那个文件、不读那个 Key，只读 `AEGIS_EXT_LLM_API_KEY` 环境变量。
2. **原仓库的三个 `tools/*.py` 是 0 字节空文件**，`tools/` 目录没有 `__init__.py` —— 是占位而非实现，
   主线不并入（并入会变成"看起来有工具其实没有"的空壳）。

## 复核用

原仓库完整快照在本机临时目录（未入库）：`/tmp/hla`（Git Bash 映射到 `%TEMP%/hla`）。
需要逐文件比对时用 `git diff --no-index` 对照本目录与主线实现。
