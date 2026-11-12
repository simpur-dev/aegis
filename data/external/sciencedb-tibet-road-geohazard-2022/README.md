# 2022 年度西藏地质灾害普查数据（公路沿线）

> ⚠️ **许可：CC BY-NC 4.0（非商业）**。本目录与 AGPL-3.0 主仓库**隔离**，
> `manifest.json` 标 `commercial_use: false`；**商用发行前必须整目录移除或另行取得授权**。
> 许可全文与署名要求见 [`LICENSE-CC-BY-NC-4.0.md`](LICENSE-CC-BY-NC-4.0.md)。

Science Data Bank 数据集（DOI [`10.57760/sciencedb.27813`](https://doi.org/10.57760/sciencedb.27813)）的
本地副本与规范化版本。内容是**西藏公路沿线地质灾害普查**：崩塌 / 水毁 / 沉陷塌陷 /
泥石流 / 滑坡五类，共 **13,768 条**记录。

**它是普查台账，不是完整灾情事件库**：字段是道路与隐患点口径（线路、桩号、断通状况、
灾害等级等）；其中 **2,070 条 `是否发生灾害=否`** 是隐患点，不是已发生事件；也没有
人员伤亡、经济损失、受影响人口这些灾损字段。用它做"公路地灾隐患分布"可以，
用它算"西藏灾情损失"不行。

## 文件

| 文件 | 内容 |
|---|---|
| `raw/崩塌.csv` | 6,692 行，GB18030 + CRLF，**按下载字节原样保留** |
| `raw/水毁.csv` | 1,037 行 |
| `raw/沉陷塌陷.csv` | 1,227 行 |
| `raw/泥石流.csv` | 2,909 行 |
| `raw/滑坡.csv` | 1,903 行 |
| `normalized/tibet_road_geohazard_2022.v1.csv` | 13,768 行，UTF-8 + LF，27 列（见下） |
| `schema.json` | 规范化 CSV 的列契约（类型/枚举/范围/主键） |
| `manifest.json` | 来源、许可、DOI、SHA-256、行数、规范化口径、已知限制 |
| `LICENSE-CC-BY-NC-4.0.md` | 数据许可与商用限制 |

`raw/` 与 `normalized/` 的区别：前者是证据（编码、换行、字段一个不动），后者是可用数据
（统一编码、挑跨类型通用列、日期 ISO、坐标拆出首点）。**要原始数值查 raw，要做分析用 normalized。**

## 来源与署名

- 作者：**田建（Tian Jian），中国交通科学研究院（China Academy of Transportation Sciences）**
- 发布：Science Data Bank，V1，2025-07-18
- DOI：`10.57760/sciencedb.27813`；CSTR：`31253.11.sciencedb.27813`
- 下载入口：<https://www.scidb.cn/doi/10.57760/sciencedb.27813>
- 抓取方式：Science Data Bank 公开文件树 API（无需登录），5 个 CSV 原始字节下载；
  脚本 `backend/scripts/fetch_tibet_road_geohazard.py` 可复跑。

**引用**：

```text
田建（Tian Jian），中国交通科学研究院. 2022 年度西藏地质灾害普查数据[DS/OL]. V1.
Science Data Bank, 2025. https://doi.org/10.57760/sciencedb.27813.
CSTR:31253.11.sciencedb.27813. CC BY-NC 4.0.
```

## 规范化口径（raw → normalized）

| 规范化列 | 源字段 | 处理 |
|---|---|---|
| `record_id` | `灾害编号` | `sciencedb-tibet-road-geohazard-2022:<灾害编号>`；全表唯一 |
| `source_category` | 文件名 | 崩塌 / 水毁 / 沉陷塌陷 / 泥石流 / 滑坡 |
| `hazard_type` | 文件名 | 映射到项目 `HazardType`（见下表） |
| `disaster_code` | `灾害编号` | 去首尾空白 |
| `name` | `灾害名称` | 原值 |
| `road_class` / `road_name` / `road_code` | `道路类别` / `线路名称` / `线路编号` | 原值，去空白 |
| `admin_code` | `区划代码` | 6 位县级代码 |
| `maintainer` | `管养单位` | 原值 |
| `occurred` | `是否发生灾害` | `是`→`true`、`否`→`false`；**false = 隐患点** |
| `last_event_date` | `最近发生时间` | 统一 `YYYY-MM-DD`；解析不了留空 |
| `last_event_date_raw` | `最近发生时间` | 原文保留 |
| `blocked_status` | `断通状况` | 原值 |
| `severity` | `危害程度` | 原值 |
| `damage_level` | `灾害等级` | 原值 |
| `development_degree` | `灾害发育程度` | 原值 |
| `tech_level` | `技术等级` | 原值 |
| `occurrence_window_years` | `近十年发生次数` / `近五年发生次数` | 有"近十年"列则 `10`，否则 `5` |
| `occurrence_count` | 同上 | 原值 |
| `treatment` | `灾害处治情况` 或 `灾害修复情况` | 沉陷塌陷用"修复情况"，其余用"处治情况" |
| `check_status` | `核查状态` | 原值 |
| `start_stake` / `end_stake` | `起点桩号` / `止点桩号` | 原值 |
| `longitude` / `latitude` | `经纬度CGCS2K` | 取**第一个点**（原串是 `lon,lat;lon,lat;` 折线），6 位小数 |
| `geometry_points` | `经纬度CGCS2K` | 折线点数 |

类别映射（**沉陷塌陷与水毁在项目枚举里没有对应类，落 `unknown`，原文保留在 `source_category`**）：

| 源类别 | `hazard_type` |
|---|---|
| 崩塌 | `rockfall` |
| 滑坡 | `landslide` |
| 泥石流 | `debris_flow` |
| 沉陷塌陷 | `unknown` |
| 水毁 | `unknown` |

## 数据摘要（本副本实测）

| 口径 | 数值 |
|---|---|
| 记录数 | 13,768（崩塌 6,692 / 泥石流 2,909 / 滑坡 1,903 / 沉陷塌陷 1,227 / 水毁 1,037） |
| `occurred=true` | 11,698 |
| `occurred=false`（隐患点） | 2,070 |
| `last_event_date` 有值 | 11,712，范围 2012-07-10 — 2022-09-04 |
| `last_event_date` 为空 | 2,056（源数据未填） |
| 坐标范围 | 经度 78.716912 — 99.093165；纬度 27.251338 — 36.134342 |
| 覆盖县级区划 | 90 个（按 `admin_code` 去重） |
| 原始文件大小 | 13,009,392 字节（12.41 MB） |

## 已知限制（源数据自带，未清洗）

1. **公路口径**：只覆盖公路沿线地质灾害点，非公路地灾点不在内；不能当西藏全量灾情。
2. **隐患点≠事件**：2,070 条 `是否发生灾害=否`；统计事件时必须先过滤 `occurred=true`。
3. **日期缺失**：2,056 条最近发生时间为空；有值的日期是源单位填写的，未做跨源核验。
4. **坐标是折线**：`经纬度CGCS2K` 是多点串，normalized 只取首点；要做空间分析回 raw 解析全串。
5. **无灾损字段**：没有伤亡、经济损失、受影响人口；这些字段在本数据里不存在，不要用别的表补造。
6. **等级是源口径**：`灾害等级`/`危害程度` 是普查填报表的分级，不是平台模型输出，二者不可混用。

## 更新与校验

```bash
cd backend

# 只校验：manifest 里每个文件的 SHA-256 / 字节数 / 行数是否仍一致（CI 跑这个）
uv run python -m scripts.fetch_tibet_road_geohazard --check

# 重新下载并重写 raw / normalized / manifest（联网）
uv run python -m scripts.fetch_tibet_road_geohazard --refresh

# 用本地已下载的 5 个 CSV 重跑（不联网）
uv run python -m scripts.fetch_tibet_road_geohazard --refresh --source-dir /path/to/csv-dir
```

`raw/` 按字节原样保存（GB18030 + CRLF），`--check` 按字节校验；不要用文本编辑器打开后再保存，
否则编码/换行一变，SHA 就对不上。`normalized/` 是生成物，改它请改脚本后重跑。
