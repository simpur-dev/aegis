# 帕隆藏布流域冰湖清单（2020 / 2025 两期）

> **许可：CC BY 4.0** —— 允许署名后的**任何用途，含商业使用与再分发**（须保留署名与许可链接）。

Zenodo 发布的两期冰湖清单，覆盖**帕隆藏布流域**（雅鲁藏布江最大支流，藏东南林芝—米林—墨脱一带）。

**为什么收它**：平台的五灾种里有**冰湖溃决（lake_outburst）**，但此前仓里**没有任何真实冰湖点位数据**
——NASA GLC 只有滑坡/泥石流，公路普查只有公路沿线五类。冰湖本体清单补的就是这一块，
而且是**两期**，可以算湖群扩张（溃决风险的底层变量）。

## 文件

| 文件 | 内容 |
|---|---|
| `raw/parlung_zangbo_glacial_lakes.zip` | Zenodo 原始交付 ZIP（1,371,267 字节） |
| `raw/parlung_glacial_lakes_2020.xlsx` | 2020 期原始表（1,618 条） |
| `raw/parlung_glacial_lakes_2025.xlsx` | 2025 期原始表（1,606 条） |
| `normalized/parlung_zangbo_glacial_lakes.v1.jsonl` | 3,224 条规范化记录 |
| `manifest.json` | 来源/许可/逐文件 SHA-256/行数/字段映射/已知限制 |

## 来源与署名

- `Glacial Lake Inventory of the Parlung Zangbo Basin` —— Zenodo 记录 `20305493`，
  DOI [`10.5281/zenodo.20305493`](https://doi.org/10.5281/zenodo.20305493)（CC BY 4.0）
- 抓取：Zenodo REST API（`/api/records/<id>/files/datasets.zip/content`），**匿名可下**。

## 数据摘要（本副本实测）

| 口径 | 数值 |
|---|---|
| 记录数 | **3,224**（2020 期 1,618 / 2025 期 1,606） |
| 经度范围 | 92.9860 — 97.0876 |
| 纬度范围 | 29.1705 — 31.0296 |
| 区域 | 帕隆藏布流域（藏东南） |

## 每条记录带的属性

| 字段 | 含义 | 用途 |
|---|---|---|
| `surface_area_km2` | 湖面面积 | 溃决规模的主控变量 |
| `volume_1e6_m3` | 蓄水量（2025 期才有） | 溃决潜在释放量 |
| `elevation_min_m` / `max_m` / `mean_m` | 水面高程区间 | 与下游高差决定溃决影响范围 |
| `slope_min_deg` / `max_deg` / `avg_deg` | 周边坡度 | 稳定性与爬升条件 |
| `nearest_glacier_m` | 最近冰川距离 | 冰川补给/冰崩诱发条件 |
| `glacier_contact` | 是否与冰川接触（`Yes`/`No`） | 溃决最常见的触发路径 |
| `lake_type` / `lake_classification` | 冰湖类型与分类 | 成因判别（moraine-dammed / supraglacial…） |
| `projected_x` / `projected_y` | 源投影坐标原值 | 需要原投影时用（normalized 用的是经纬度） |

## 规范化口径

- **坐标**：源表同时给度分秒字符串（`096° 57' 8.81" E`）与投影坐标；
  normalized 取 **DMS 解析出的十进制度**（`core.parse_dms`），投影列原值保留在
  `projected_x` / `projected_y`。
- **缺坐标的行跳过**：源表里坐标列可能为空；跳过的条数记在 manifest 的
  `normalization.missing_rule`。
- **两期字段不同**：2025 版多了体积与冰湖分类，2020 版没有 → 缺失项留 `null`，不补。

## 已知问题（源数据自带，未改）

1. **是冰湖清单，不是溃决事件**：没有溃决发生时间、伤亡、损失字段。
2. **只覆盖帕隆藏布流域**，不覆盖全藏冰湖（全藏见 `tpdc-himalaya-geohazard-2006-2009`
   的 19 个溃决隐患终碛湖，但那是"隐患调查"不是"冰湖全集"）。
3. **两期口径不完全一致**：2025 版有体积/分类/是否接冰川，2020 版没有，
   跨期比较只能比面积与位置。
4. **未做溃决概率评估**：清单本身不判断哪一个湖会溃——那是模型的事，本目录只提供底数。

## 更新与校验

```bash
cd backend

uv run python -m scripts.external_data --check --dataset zenodo-parlung-zangbo-glacial-lakes
uv run python -m scripts.external_data --refresh --dataset zenodo-parlung-zangbo-glacial-lakes
```
