# 喜马拉雅大型滑坡清单（Zenodo 两套）

> **许可：CC BY 4.0** —— 允许署名后的**任何用途，含商业使用与再分发**（须保留署名与许可链接）。
> 这是本目录里**唯一没有非商业限制**的喜马拉雅地灾数据源，可直接进对外交付物。

两个来源合成一个数据集目录（同一个 DOI 家族、同一个研究区域、同一种"空间清单"口径）：

| 子集 | 内容 | 条数 | 坐标 |
|---|---|---|---|
| `eastern-himalaya` | 东喜马拉雅大型滑坡点位（KML） | **420** | 有（WGS84） |
| `megas` | 全球巨型滑坡目录（≥1 km³ 或 ≥10 km²） | **411** | **无**（只有地名） |

## 为什么是"清单"不是"事件"

两套都是**空间清单**：告诉你"这里有一个滑坡体"，但**不告诉你什么时候滑的、滑了多大、
死了多少人**。所以本目录的记录里 `event.date` 与 `impact.fatalities` 全是 `null`——
不是漏了，是源数据就没有。

要配事件目录用 `nasa-glc`（历史事件）或 `sciencedb-tibet-road-geohazard-2022`（公路普查）。

## 文件

| 文件 | 内容 |
|---|---|
| `raw/eastern_himalaya_large_landslides.kml` | 原始 KML（420 个 Placemark） |
| `raw/megas_master_2025.csv` | 原始 CSV（411 行，**cp1252 编码**） |
| `normalized/eastern_himalaya_large_landslides.v1.jsonl` | 420 条规范化记录 |
| `normalized/megas_largest_landslides.v1.jsonl` | 411 条规范化记录 |
| `manifest.json` | 来源/许可/逐文件 SHA-256/行数/字段映射/已知限制 |

## 来源与署名

- `Large Landslide Inventory of the Eastern Himalaya` —— Zenodo 记录 `18931430`，
  DOI [`10.5281/zenodo.18931430`](https://doi.org/10.5281/zenodo.18931430)（CC BY 4.0）
- `Earth's largest terrestrial landslides (MEGAS)` —— Zenodo 记录 `15908068`，
  DOI [`10.5281/zenodo.15908068`](https://doi.org/10.5281/zenodo.15908068)（CC BY 4.0）

抓取：Zenodo REST API（`/api/records/<id>/files/<name>/content`），**匿名可下**。
作者与标题从 API 的 `metadata` 取，不手写。

## 数据摘要（本副本实测）

| 口径 | 数值 |
|---|---|
| 东喜马拉雅点位数 | **420**（编号 `EH-1 … EH-420`） |
| 东喜马拉雅范围 | 经度 90.4011 — 97.1582；纬度 27.0330 — 30.2959 |
| MEGAS 条数 | **411**（含中国 **26** 条） |
| MEGAS 中国条目（部分） | Big Dragon、Daguangbao、Danba、Dehenglong、Dongchuan、Jiaobuong、Kangyang、Qiaojia、Suozi、Xijitan、Nyingchi/FID57… |
| MEGAS 有坐标的条目 | **0**（源 CSV 只有 `Location` 地名） |
| MEGAS 源数据缺 ID 的行 | **14**（含 6 条中国记录） |

## 规范化口径

- **东喜马拉雅**：KML `<Placemark>` → `<name>` 取编号、`<coordinates>` 取 `lon,lat`（WGS84）。
- **MEGAS**：CSV 是 **cp1252** 编码（不是 UTF-8，直接按 UTF-8 读会炸）；
  保留原列名到 `hazard.attributes`（岩石类型、滑坡类型、堆积体面积/体积、距活动断裂距离、
  地形位置）。
- **缺 ID 的 14 行**：源 CSV 缺 `ID`，其中 6 条是中国记录（Dehenglong / Jiaobuong /
  Kangyang / Qiaojia2 / Suozi / Xijitan）。按 ID 过滤会把它们**悄悄丢掉**，
  所以这 14 条按行序编号（`megas:row-0121`），`source.event_id` 留 `null`。
- **坐标**：MEGAS 无坐标 → `geo.latitude` / `geo.longitude` 留 `null`，**不用地名猜坐标**。

## 已知问题（源数据自带，未改）

1. **MEGAS 无坐标**：411 条只有地名。需要坐标时另做地名关联（gazetteer），
   本目录不臆造。
2. **没有日期**：两套都没有事件日期，滑体位置不代表发生时刻。
3. **覆盖偏东南**：东喜马拉雅清单在经度 90.4–97.2、纬度 27.0–30.3（藏东南—尼泊尔东部），
   **不覆盖阿里/日喀则西段**；喜马拉雅西段看 `tpdc-himalaya-geohazard-2006-2009`。
4. **规模属性只有 MEGAS 有**（面积/体积），东喜马拉雅清单只有点位。

## 更新与校验

```bash
cd backend

uv run python -m scripts.external_data --check --dataset zenodo-himalaya-landslide-inventories
uv run python -m scripts.external_data --refresh --dataset zenodo-himalaya-landslide-inventories
```
