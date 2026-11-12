# NASA Global Landslide Catalog（中国 / 西藏子集）

NASA 哥达德太空飞行中心维护的全球降雨触发滑坡事件目录（GLC）的静态导出快照，
本目录保存**中国子集**与**西藏子集**的原始投影与规范化版本。

**这不是西藏灾情的完整普查**：GLC 只收降雨触发的滑坡/泥石流事件，以媒体报道为线索，
没有报道的事件不在里面；快照的事件日期止于 **2016-06**。用它做历史基线可以，
用它宣称"西藏滑坡全量"或"平台预测准确率"不行。

## 文件

| 文件 | 内容 |
|---|---|
| `raw/glc_export_rows.china.csv` | 源数据 `country_name == "China"` 的 426 行，**原始 31 列一个不改** |
| `raw/glc_export_rows.tibet_admin_labeled.csv` | 其中 `admin_division_name == "Tibet Autonomous Region"` 的 18 行（含 2 条源标注错误，见下） |
| `normalized/china_landslide_events.v1.jsonl` | 426 条规范化记录 |
| `normalized/tibet_landslide_events.v1.jsonl` | 16 条规范化记录（已排除 2 条源标注错误） |
| `schema.json` | 单条记录的 JSON Schema（与 `backend/tests/unit/test_external_data_catalog.py` 联动） |
| `manifest.json` | 来源 URL、SHA-256、抓取时间、行数、选择规则、排除项 |

## 来源与许可

- **来源**：`https://data.nasa.gov/docs/legacy/Global_Landslide_Catalog_Export/Global_Landslide_Catalog_Export_rows.csv`
  （NASA Open Data Portal，静态导出快照；全表 11,033 行、8,479,717 字节、SHA-256 见 `manifest.json`）
- **许可**：**public domain**。NASA 作品不受美国版权保护，可再分发；NASA 请求引用（非法律义务）。
- **上游现状**：COOLR / Landslide Viewer（`https://landslides.nasa.gov`）是持续更新的版本；
  本目录用的是 data.nasa.gov 的冻结快照，事件日期止于 2016-06。要更新到近年数据需走 COOLR，
  且需另行核对导出格式与许可。

**引用**（对外发表时请同时引用）：

> Kirschbaum, D. B., Adler, R., Hong, Y., Hill, S., & Lerner-Lam, A. (2010).
> A global landslide catalog for hazard applications: method, results, and limitations.
> *Natural Hazards*, 52(3), 561–575. doi:10.1007/s11069-009-9401-4

## 选择规则

```text
中国：country_name == "China"                                  → 426 行
西藏：country_name == "China" and
      admin_division_name == "Tibet Autonomous Region"          → 18 行（raw）
      排除 2 条源标注错误后                                        → 16 行（normalized）
```

排除项（写死在抓取脚本 `TIBET_EXCLUSIONS`，可复核）：

| event_id | 原因 |
|---|---|
| `4392` | `admin_division_name` 标 Tibet，但 location 为 **Dibang, Arunachal Pradesh（印度）** |
| `7108` | `admin_division_name` 标 Tibet，但 location 为 **Tatopani/Araniko Highway（尼泊尔）**，属 2015 尼泊尔地震 |

`raw/` 保留源标注（18 行），`normalized/` 才是可用数据（16 行）——**不静默改正源数据，也不把错标当事实**。

## 字段映射（raw → normalized）

| 规范化字段 | 来源列 | 说明 |
|---|---|---|
| `record_id` | `event_id` | `nasa-glc:<event_id>` |
| `source.dataset` | — | 常量 `nasa-glc` |
| `source.source_name` | `source_name` | 源目录记录的信息提供方（多为媒体），不是 NASA |
| `source.source_url` | `source_link` | 原始报道链接；失效链接保留原值，不替换 |
| `source.license` | — | 常量 `public-domain` |
| `event.date` | `event_date` | 只取日期部分（`MM/DD/YYYY` → `YYYY-MM-DD`） |
| `event.time_raw` | `event_date` 的时刻部分 | **原文保留，不做时区换算**；`12:00:00 AM` 多为占位零点，不是实测时刻 |
| `event.title` / `description` / `location` | 同名列 | 原文，不清洗、不翻译 |
| `event.location_accuracy` | `location_accuracy` | 源给的精度档（`exact` / `1km` / `5km` / … / `unknown`） |
| `event.storm_name` | `storm_name` | 台风相关事件才有 |
| `event.notes` | `notes` | 源目录备注 |
| `geo.latitude` / `longitude` | `latitude` / `longitude` | 浮点数；本子集无缺坐标记录 |
| `geo.country` / `admin_division` | 同名列 | 源标注，未做边界校正 |
| `hazard.type` | `landslide_category` | 映射见下表 |
| `hazard.source_category` | `landslide_category` | 保留原值，映射可逆 |
| `hazard.trigger` / `size` / `setting` | 同名列 | 原值（含 `unknown`） |
| `impact.fatalities` / `injuries` | `fatality_count` / `injury_count` | **空字符串 → `null`（未报）；`0` → `0`（明确报了零）** |

类别映射：

| GLC category | 项目 `HazardType` |
|---|---|
| `landslide` / `translational_slide` | `landslide` |
| `mudslide` / `debris_flow` | `debris_flow` |
| `rock_fall` | `rockfall` |
| `complex` | `landslide`（复合型滑坡；原值保留在 `source_category`） |
| `other` / `unknown` / 空 | `unknown` |

## 数据摘要（本快照实测）

| 口径 | 中国 | 西藏 |
|---|---|---|
| 记录数 | 426 | 16 |
| 日期范围 | 2007-01-19 — 2016-10-06 | 2008-07-25 — 2016-06-18 |
| 类型分布 | landslide 338 / debris_flow 78 / rockfall 7 / unknown 3 | landslide 14 / debris_flow 2 |
| 死亡人数合计（仅已报记录） | 4,945（366 条有值，60 条未报） | 83（10 条有值，6 条未报） |
| 受伤人数合计（仅已报记录） | 318（119 条有值） | 1（5 条有值） |

合计只加**明确报了数**的记录，未报不等于零——这是空值语义，不是统计口径。

## 已知质量问题（源数据自带，未清洗）

1. **`admin_division_name` 会错标**：如 `4392`（印度）与 `7108`（尼泊尔）被标成 Tibet。
   已在西藏 normalized 中排除；中国子集按源 `country_name` 过滤，未做边界校正。
2. **坐标精度有限**：大量记录是 `50km` / `25km` / `10km` 档，不是点位级；用前看 `location_accuracy`。
3. **时间不可靠**：`12:00:00 AM` 常见为占位零点；时区未声明，本目录不做换算。
4. **媒体源偏差**：有报道的事件才进目录；无人区、无通讯、无伤亡的事件系统性缺失。
5. **触发只含降雨相关**：GLC 以降雨触发为主，地震触发仅 7 条（中国子集）；冰雪融水 4 条。
   雪崩与冰湖溃决不在本目录覆盖内（项目支持这两类，但这份数据没有）。

## 更新与校验

```bash
cd backend

# 只校验：manifest 里每个文件的 SHA-256 / 字节数 / 行数是否仍一致（CI 跑这个）
uv run python -m scripts.fetch_external_disaster_data --check

# 重新抓取并重写 raw / normalized / manifest（联网；或 --input 指定已下载的源 CSV）
uv run python -m scripts.fetch_external_disaster_data --refresh

# 用本地副本重跑（不联网）
uv run python -m scripts.fetch_external_disaster_data --refresh --input /path/to/rows.csv
```

`--refresh` 会改写 `manifest.json`（含源文件 SHA-256 与抓取时间）。源文件变了要**人看 diff**：
条数、日期范围、排除项是否仍成立，再提交。`--check` 与
`backend/tests/unit/test_external_data_catalog.py` 会把"数据被手改过"钉成红。
