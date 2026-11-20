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
    ├── schema.json              # 规范化记录的契约（JSON Schema；七个数据集共用同一份统一信封）
    ├── raw/                     # 按选择规则过滤后的**原始列/原始字节**投影（不改字段、不补值）
    └── normalized/              # 项目口径数据（字段见 schema.json；空值一律 null/空）
```

三个约定：

- `raw/` 与 `normalized/` **不互相替代**：前者是证据，后者是可用数据。字段映射只在脚本里做一次。
- 校验和以 `manifest.json` 为准，`--check` 逐项比对；改数据必须重跑 `--refresh` 并复核 diff。
- 抓取脚本在 `backend/scripts/external_data/`（数据目录只放数据与说明）：
  **规矩只有一份**（`core.py`：清单/校验和/规范化写盘/下载），**每个数据集一个模块**
  （`nasa_glc` / `pangaea_debris_flow` / `scidb_census` / `tpdc_himalaya` / `zenodo_himalaya` / `zenodo_lakes` / `glof_db`）。
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
| [`glof-db`](glof-db/README.md) | 全球冰湖溃决**事件**库 V3.1（千年尺度，含机制/体积/洪峰/伤亡） | **历史事件** | **CC BY 4.0** | 3,151 | 全球 8 个冰川区；3,062 条有坐标；中国 107 条（HMA 表）；**补上 `lake_outburst` 事件维度** |
| [`tpdc-himalaya-geohazard-2006-2009`](tpdc-himalaya-geohazard-2006-2009/README.md) | 喜马拉雅重大地灾调查点位（泥石流/滑坡/溃决隐患冰湖） | **灾害本体 + 形态学属性** | **CC BY-NC-SA 4.0** | 525 | 喜马拉雅山全段（78.9–95.3°E）；坡度/比降/发育阶段/冰湖成灾风险 |
| [`zenodo-parlung-zangbo-glacial-lakes`](zenodo-parlung-zangbo-glacial-lakes/README.md) | 帕隆藏布流域冰湖清单（2020 / 2025 两期） | **灾害本体** | **CC BY 4.0** | 3,224 | 藏东南（93.0–97.1°E）；面积/体积/坡度/与冰川接触 |
| [`zenodo-himalaya-landslide-inventories`](zenodo-himalaya-landslide-inventories/README.md) | 东喜马拉雅大型滑坡 420 点 + 全球巨型滑坡 MEGAS 411 条 | **灾害本体** | **CC BY 4.0** | 831 | 藏东南—尼泊尔东部；MEGAS 中国 26 条 |
| [`sciencedb-tibet-road-geohazard-2022`](sciencedb-tibet-road-geohazard-2022/README.md) | 西藏公路地质灾害普查（崩塌/水毁/沉陷塌陷/泥石流/滑坡） | **事件 + 隐患点** | **CC BY-NC 4.0** | 13,768 | 公路沿线普查；事件时间 2012-07 至 2022-09 |
| [`nasa-glc`](nasa-glc/README.md) | NASA 全球滑坡目录（中国 / 西藏子集） | **历史事件** | public domain（NASA） | 中国 426 / 西藏 16 | 2007-01 至 2016-10；以降雨触发为主（另有地震 7 / 采矿 6 / 融雪 4 等），媒体源 |
| [`pangaea-debris-flow-magnitude`](pangaea-debris-flow-magnitude/README.md) | 全球泥石流规模目录（体积/面积/火后/成因） | **规模编目（非事件）** | **CC BY 3.0** | 988 | 全球；无坐标，只有地名与文献出处 |

**现在仓里能回答的问题**（说明为什么是这七套）：

- "**某地有没有滑坡体**" → tpdc + zenodo 两套点位；
- "**冰湖在哪、多大、离冰川多远**" → zenodo 冰湖清单（3,224 个，两期可比较）；
- "**冰湖什么时候溃过、怎么溃的、后果如何**" → GLOF DB 事件库（3,151 条，全球千年尺度）；
- "**滑坡的坡度/地层/泥石流的比降与发育阶段**" → tpdc 的专业属性；
- "**发生过什么、死了多少人**" → 公路普查 + NASA GLC + GLOF DB；
- "**一次泥石流有多大（体积/面积/是否火后）**" → PANGAEA 规模目录（988 条，无坐标）；
- "**触发因子（降雨）**" → **目录里没有**，但**平台已经接上了外部降水**：
  TPDC 全国 0.1° 格点降水 V2（**CC BY 4.0**，逐日 1960–2025，全藏，46.8 GB）落**运行态目录**
  （`backend/data/precip/`，不进 git），取件与派生由
  `backend/scripts/fetch_precip_chmpre.py` → `backend/scripts/derive_precip_station_daily.py`
  两步产出站点逐日表，平台侧由 `precip` 腿读它（默认关）。
  **为什么不入这个目录**：它是**气象驱动数据**（不是灾情记录）、体量大，两个理由都指向
  "落运行态"而不是"跟仓库发布"——这与本目录的准入规则并不冲突，是两类东西。
  另：**小时雨强（`rainfall_mm_h`）仍无真实源**——格点日值只能给 24 小时累计，两者口径不同、
  禁止近似折算（详见 `docs/AGENT_PROGRESS.md` 的未清项）。

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
| 中国逐日 0.1° 格点降水 V2（TPDC，`10.11888/Atmos.tpdc.300523`；Zenodo 镜像 `10.5281/zenodo.20378336`） | **CC BY 4.0**，匿名 FTP / HTTPS 均可下 | **已接进平台，但不入仓**（2026-11-17）：逐日 1960–2025、全藏、**46.8 GB**。它是气象驱动数据不是灾情数据，且体量不该进 git——落**运行态目录** `backend/data/precip/`，由 `fetch_precip_chmpre.py` 按年取、`derive_precip_station_daily.py` 派生站点逐日表。**本机取数通道实测不通**（Zenodo 限流、TPDC FTP 421 并发上限），派生表尚未产出 |
| 雅鲁藏布江流域逐日 10km 降水（TPDC，`10.11888/Hydro.tpdc.270917` V1 / `10.11888/Atmos.tpdc.272885` V2） | CC BY-NC-SA 4.0，HTTP/FTP 可下（**V1 306 MB / V2 469 MB**） | 覆盖对口（含帕隆/墨脱），但**本轮不收**：① 分辨率无优势——CHM_PRE V2 的 0.1° 在 30°N 约合 11 km，与 10 km 同量级；② CHM_PRE V2 许可干净（CC BY 4.0）且覆盖全藏；③ NC-SA 的 ShareAlike 会把 `normalized/` 派生物一并染上 NC-SA。**体量记录已更正**：此前记的"29–62 MB"是错的 |
| 雅鲁藏布江高时空分辨率降水（3h / 5km，1981–2019，Zenodo `10.5281/zenodo.4674289`） | **许可口径冲突**：TPDC 标 CC BY-NC-SA 4.0、Zenodo 标 CC BY 4.0 | **入仓前必须向作者书面澄清**；按最严的 NC-SA 处理 |
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
- **触发因子（降雨）走的是另一条路**：不进本目录、落运行态目录，见上面"现在仓里能回答的
  问题"那一节与 `backend/scripts/fetch_precip_chmpre.py`。**小时雨强仍无真实源**——
  格点日值只能给 24 小时累计，两者口径不同、禁止近似折算。
- 数据集之间的口径不可直接合并（事件定义、时空粒度、去重规则都不同），
  要合并先在 `normalized/` 之上另做映射层。
