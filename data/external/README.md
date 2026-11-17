# 外部公开灾情数据

这个目录只放**从公开来源抓取、许可允许再分发**的灾情/灾害数据，随仓库发布。
运行态数据（遥测、预警、模型权重、反馈记忆）仍在 `backend/data/`，不入库。

## 准入规则（四条，缺一不收）

1. **默认只收许可允许再分发的数据**：public domain / CC0 / CC BY 优先。`NC`（非商业）、
   `ND`（禁止演绎）、需登录或需书面授权的一律不收——本仓库是 AGPL 开源仓库，下游可能商用。
   **例外通道**（须项目负责人明确批准）必须同时满足四条：① 独立目录；② 独立许可文件；
   ③ `manifest.json` 标 `commercial_use: false` 并写明"商用发行前移除"；④ 不与主代码或
   public-domain 数据混放。**当前走这条通道的有两个**：`sciencedb-tibet-road-geohazard-2022`
   （CC BY-NC 4.0）与 `tpdc-himalaya-geohazard-2006-2009`（CC BY-NC-SA 4.0）。
2. **每条记录能回到来源**：保留原始事件号与来源 URL；抓取脚本、源文件 SHA-256、抓取时间写进
   `manifest.json`，任何人可复跑核验。
3. **只收结构化事件数据**：能落成"时间 + 地点 + 类型 + 影响"的记录；新闻通稿、PDF 报告、
   需要 OCR 的扫描件不收（宁缺毋滥）。
4. **不伪装完整**：源数据是抽样/目录/媒体汇编的，必须在数据集 README 里写明覆盖范围与偏差，
   不写成"全量灾情库"。

## 目录规范

```
data/external/
├── README.md                    # 本文件：准入规则、目录规范、数据集索引
└── <来源-slug>/
    ├── README.md                # 该数据集：来源、许可、字段映射、已知质量问题、更新方法
    ├── LICENSE-*.md             # 仅 NC/特殊许可需要：独立许可摘要与商用限制
    ├── manifest.json            # 机器可读元数据：来源/许可/抓取时间/校验和/条数/选择规则
    ├── schema.json              # 规范化记录的契约（JSON Schema；五个数据集共用同一份统一信封）
    ├── raw/                     # 按选择规则过滤后的**原始列/原始字节**投影（不改字段、不补值）
    └── normalized/              # 项目口径数据（字段见 schema.json；空值一律 null/空）
```

三个约定：

- `raw/` 与 `normalized/` **不互相替代**：前者是证据，后者是可用数据。字段映射只在脚本里做一次。
- 校验和以 `manifest.json` 为准，`--check` 逐项比对；改数据必须重跑 `--refresh` 并复核 diff。
- 抓取脚本在 `backend/scripts/external_data/`（数据目录只放数据与说明）：
  **规矩只有一份**（`core.py`：清单/校验和/规范化写盘/下载），**每个数据集一个模块**
  （`nasa_glc` / `scidb_census` / `tpdc_himalaya` / `zenodo_himalaya` / `zenodo_lakes`）。
  统一入口：

  ```bash
  cd backend
  uv run python -m scripts.external_data --check                    # 校验全部
  uv run python -m scripts.external_data --refresh                  # 重抓全部
  uv run python -m scripts.external_data --refresh --dataset <id>   # 只动一个
  ```

## 数据集索引

按"平台要什么"排：先是要**触发因子**（暴雨），再是**灾害本体**（滑坡/泥石流/冰湖在哪），
最后才是**事件**（什么时候发生、死了多少人）。

| id | 内容 | 性质 | 许可 | 记录数 | 覆盖 |
|---|---|---|---|---|---|
| [`tpdc-himalaya-geohazard-2006-2009`](tpdc-himalaya-geohazard-2006-2009/README.md) | 喜马拉雅重大地灾调查点位（泥石流/滑坡/溃决隐患冰湖） | **灾害本体 + 形态学属性** | **CC BY-NC-SA 4.0** | 525 | 喜马拉雅山全段（78.9–95.3°E）；坡度/比降/发育阶段/冰湖成灾风险 |
| [`zenodo-parlung-zangbo-glacial-lakes`](zenodo-parlung-zangbo-glacial-lakes/README.md) | 帕隆藏布流域冰湖清单（2020 / 2025 两期） | **灾害本体** | **CC BY 4.0** | 3,224 | 藏东南（93.0–97.1°E）；面积/体积/坡度/与冰川接触 |
| [`zenodo-himalaya-landslide-inventories`](zenodo-himalaya-landslide-inventories/README.md) | 东喜马拉雅大型滑坡 420 点 + 全球巨型滑坡 MEGAS 411 条 | **灾害本体** | **CC BY 4.0** | 831 | 藏东南—尼泊尔东部；MEGAS 中国 26 条 |
| [`sciencedb-tibet-road-geohazard-2022`](sciencedb-tibet-road-geohazard-2022/README.md) | 西藏公路地质灾害普查（崩塌/水毁/沉陷塌陷/泥石流/滑坡） | **事件 + 隐患点** | **CC BY-NC 4.0** | 13,768 | 公路沿线普查；事件时间 2012-07 至 2022-09 |
| [`nasa-glc`](nasa-glc/README.md) | NASA 全球滑坡目录（中国 / 西藏子集） | **历史事件** | public domain（NASA） | 中国 426 / 西藏 16 | 2007-01 至 2016-10；以降雨触发为主（另有地震 7 / 采矿 6 / 融雪 4 等），媒体源 |

**现在仓里能回答的问题**（说明为什么是这五套）：

- "**某地有没有滑坡体**" → tpdc + zenodo 两套点位；
- "**冰湖在哪、多大、离冰川多远**" → zenodo 冰湖清单（3,224 个，两期可比较）；
- "**滑坡的坡度/地层/泥石流的比降与发育阶段**" → tpdc 的专业属性；
- "**发生过什么、死了多少人**" → 公路普查 + NASA GLC；
- "**触发因子（降雨）**" → **仍然缺**：TPDC 有两套逐日降水——全国 0.1° 格点降水 V2
  （CC BY 4.0，46.8 GB，许可干净但体量大）与雅鲁藏布江流域 10km 降水（CC BY-NC-SA 4.0，
  29–62 MB，覆盖对口但许可含 NC）。两者都是**气象驱动数据**而非灾情数据，本轮均未收（见下）。

## 许可例外（非默认准入）

| 数据集 | 许可 | 为什么收 | 商用约束 |
|---|---|---|---|
| `sciencedb-tibet-road-geohazard-2022` | **CC BY-NC 4.0** | 项目负责人明确批准：该数据是西藏公路地灾最完整的公开普查（1.38 万条），只用于非商业研究 | **商用发行前必须整目录移除**，或在 Science Data Bank 另行取得商业授权；`manifest.json` 已标 `commercial_use: false` |
| `tpdc-himalaya-geohazard-2006-2009` | **CC BY-NC-SA 4.0** | 同上：它是唯一带**专业形态学属性**的喜马拉雅地灾点位数据（坡度/比降/发育阶段/成灾风险），是因子引擎的现成先验 | 同上；且**演绎物须沿用同一许可**（`normalized/` 即演绎物） |

## 未收清单（看过但**没有**入仓）

| 数据集 | 许可 / 访问条件 | 不收的原因 |
|---|---|---|
| High Mountain Asia Landslide Catalog V002（NSIDC，DOI `10.5067/E8U4F9M2NCCN`） | 需 NASA Earthdata Login（实测：GET → 401 → `urs.earthdata.nasa.gov`） | 无法匿名下载 |
| Global Landslide Nowcast v1.1 / v2.0.0（NASA GES DISC） | 需 Earthdata Login；**且只到 2021-02** | 既要登录又已过时；连续体 465 MB/日，不适合入库 |
| HMA Daily 5km Landslide Hazard Indicator（NSIDC） | 需 Earthdata Login | 同上；2024–2026 只有气候投影不是实况 |
| 中国逐日 0.1° 格点降水 V2（TPDC，`10.11888/Atmos.tpdc.300523`） | **CC BY 4.0**，FTP 可下 | **许可干净、体量 46.8 GB**：是触发因子的正解，但它是气象驱动数据不是灾情数据，且不该整包进 git。取用方式：`data.tpdc.ac.cn` 的 FTP 接口按年取，落到运行态数据目录 |
| 雅鲁藏布江流域逐日 10km 降水（TPDC，`10.11888/Hydro.tpdc.270917`） | CC BY-NC-SA 4.0，HTTP 可下（29–62 MB） | 覆盖正好是雅鲁藏布江流域但许可含 NC 且体积偏大；等需要降雨触发因子的那一轮再定 |
| 青藏高原全区域 250m 地质灾害危险性基础数据（TPDC，`10.11888/HumanNat.tpdc.301572`） | **CC BY 4.0**，FTP 可下（59 MB RAR，内含 51 MB GeoTIFF） | 许可干净、正是全藏危险性栅格，但**59 MB 二进制栅格进 git 不合适**；应落运行态目录并由脚本按需取 |
| EM-DAT（emdat.be） | 需注册；条款**禁止再分发** | 不能入仓。GDACS 的 API 里有 EM-DAT 镜像（含西藏事件与死亡数），但那是未公开接口且同样受 EM-DAT 条款约束——**只作查证线索，不作数据源** |
| Copernicus EMS Rapid Mapping | 匿名可下（JSON 事件表 + 矢量 ZIP） | 机制好用但**地理不对**：1,069 次激活里 28 次 mass movement 全在欧洲/中亚/非洲，**零次西藏滑坡** |
| 国家地球系统科学数据中心（geodata.cn） | 检索匿名、下载需实名 CAS 登录 | 取不到可核验的文件；条款还写"仅限科学研究使用" |
| 中国气象数据网（data.cma.cn） | 目录匿名、数据需登录 | 同上 |

以后要收新数据集，先回答三个问题：许可允许再分发吗（NC 例外是否已按上面四条隔离）？
能匿名复跑下载吗？覆盖范围与偏差写清了吗？三个都是"是"再进目录。

## 使用约束

- 这些是**历史事件 / 普查 / 点位**数据，不是实时预警源，也不是完整灾情库；
  不得用它们宣称平台的预测能力或准确率。
- **CC BY-NC / NC-SA 的例外数据集不得用于商业目的**；商用发行前整目录移除（见上表）。
- **当前未接入任何生产链路**（不进预警/研判/知识库）。要用得先写导入适配并补契约测试，
  不能直接把 JSONL/CSV 塞进 `agents/hazards/` 或 `knowledge/data/`。
- 引用要求见各数据集 README；对外发表时同时引用原始数据提供方。
- **触发因子（降雨）还缺**：见"未收清单"里的 TPDC 降水数据集——它是气象驱动数据、体量大，
  应该落运行态目录而不是 git。
- 数据集之间的口径不可直接合并（事件定义、时空粒度、去重规则都不同），
  要合并先在 `normalized/` 之上另做映射层。
