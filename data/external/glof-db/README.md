# 全球冰湖溃决事件库（GLOF DB V3.1）

> **许可：CC BY 4.0** —— 允许署名后的再分发与**商业使用**。

Lützow, Veh & Korup (2023, ESSD) 维护的**全球冰湖溃决洪水（GLOF）事件库**：3,151 条事件，
按 8 个冰川区分为 8 张工作表，每条给地点、日期（部分只到年）、溃决机制、湖泊类型、
湖泊/洪水体积与洪峰流量（部分为估算）、影响与死亡人数、参考文献。

## 它补的是哪一个空白

平台五灾种里有 `lake_outburst`，但此前仓里只有**冰湖清单**（帕隆藏布 3,224 个湖，`zenodo-parlung-zangbo-glacial-lakes`）
与 TPDC 的 **19 个溃决隐患湖**——**没有溃决事件**。本目录补的正是事件维度：
"什么时候、哪个湖、怎么溃的、后果如何"。

| | 本目录 | `zenodo-parlung-zangbo-glacial-lakes` | `tpdc-himalaya-geohazard-2006-2009` |
|---|---|---|---|
| 性质 | **溃决事件**（全球） | 冰湖**清单**（两期） | 溃决**隐患点** |
| 记录 | 3,151 条事件（千年尺度） | 3,224 个湖 | 19 个湖 |
| 坐标 | 3,062 条有（约 97%） | 全部有 | 全部有 |
| 时间 | 有事件日期（部分只到年） | 清单年份 2020/2025 | 调查期 2006–2009 |

## 文件

| 文件 | 内容 |
|---|---|
| `raw/glofdatabase_V3-1.ods` | 原始 ODS（8 张区域表，按字节保留） |
| `normalized/glof_events.v1.jsonl` | 3,151 条规范化记录（统一信封） |
| `schema.json` | 单条记录的 JSON Schema（统一信封） |
| `manifest.json` | 来源/许可/逐文件 SHA-256/行数/读取栈/已知限制 |

## 来源与署名

- 作者：**Lützow, Natalie; Veh, Georg; Korup, Oliver**
- 论文：*A global database of historic glacier lake outburst floods*, **ESSD 15, 2983–3000 (2023)**
  —— <https://doi.org/10.5194/essd-15-2983-2023>
- 数据：Zenodo 记录 `7924369` —— <https://doi.org/10.5281/zenodo.7924369>
- 文件：`glofdatabase_V3-1.ods`（685,048 字节，8 张工作表）
- 许可：**CC BY 4.0**（Zenodo API 实测 `license.id=cc-by-4.0`，匿名可下）

**引用**：

```text
Lützow, N., Veh, G., & Korup, O. (2023). A global database of historic glacier lake
outburst floods. Earth System Science Data, 15, 2983–3000.
https://doi.org/10.5194/essd-15-2983-2023
Database: https://doi.org/10.5281/zenodo.7924369 (CC BY 4.0).
```

**只收编译后的库，不拆底层湖泊清单**：库的 README 明说底层湖泊清单来自各自来源、
可能另有许可。本目录只分发这本来就以 CC BY 4.0 发布的**编译成果**。

## 一个必须说清的差异：3,151 ≠ 3,167

外部交付的标准化 CSV 标称 3,167 条。逐表核对发现它把每张表的
**字段说明行 + 示例行**（8 表 × 2 行 = 16 条"幽灵记录"）也当成了数据。

本目录直接读原始 ODS，**跳过每表前 3 行**（表头 / 字段说明 / 示例行），得 **3,151 条**；
其中 **3,062 条有坐标**（与库文档一致：约 3% 无坐标）。这个数字是可复算的：
`--check` 会核对 manifest 里的行数与 SHA-256。

## 字段映射

| 信封位置 | 源列 | 说明 |
|---|---|---|
| `record_id` | — | `glof-db:<区域slug>:<ID>`（如 `glof-db:andes:1`） |
| `source.event_id` | `ID` | `区域slug:ID`（ID 在各表内唯一） |
| `event.date` | `Date` | **能确定到日才写** ISO；只有年份写 `attributes.year`，不臆造月日 |
| `event.date_raw` | `Date` | 原文（可能是年份、区间或 `e.g.` 模板值以外的写法） |
| `event.title` | `Lake` | 湖泊名 |
| `event.description` | `Further_comments` | 备注（如 "earthquake triggered"） |
| `event.location` | `Country` | 国家名 |
| `geo.*` | `Longitude` / `Latitude` | 必须成对且为数值才写，否则 lat/lon 均 null |
| `hazard.type` | — | 常量 `lake_outburst` |
| `hazard.source_category` | — | 常量 `glof` |
| `hazard.attributes.database_region` | 工作表名 | 8 个冰川区之一 |
| `hazard.attributes.mechanism` | `Mechanism` | 溃决机制（breach / overtopping / subglacial …） |
| `hazard.attributes.lake_type` | `Lake_type` | ice / moraine / bedrock / water pocket … |
| `hazard.attributes.glacier` / `rgi_glacier_id` | `Glacier` / `RGI_Glacier_Id` | RGI 冰川编码（部分有） |
| `hazard.attributes.mean_lake_volume_vl` | `Mean_Lake_Volume_VL` | 湖泊体积（m³，部分估算） |
| `hazard.attributes.mean_flood_volume_v0` | `Mean_Flood_Volume_V0` | 洪水体积（m³，部分估算） |
| `hazard.attributes.peak_discharge_qp` | `Peak_discharge_Qp` | 洪峰流量（m³/s，部分估算） |
| `hazard.attributes.date_min_raw` / `date_max_raw` | `Date_Min` / `Date_Max` | 日期不确定区间的原文 |
| `hazard.attributes.certainty_before` / `certainty_after` | `Certainty_level_*` | 溃决前后确定性等级 |
| `hazard.attributes.reference` | `Reference` | 文献出处（**保留原文**，可能很长） |
| `hazard.attributes.place_has_coordinates` | — | 是否有坐标（3,151 中 3,062 为 true） |
| `impact.fatalities` | `reported_fatalities` | **仅 29 条有伤亡记录**，合计 3,622 人；未报为 null（不是 0） |

## 实测分布（2026-11-20 入库时）

- **区域**：NW North America 833 / Iceland 590 / High Mountain Asia 569 / European Alps 443 /
  Andes 337 / Scandinavia 192 / Greenland 153 / Other 34。
- **时间跨度**：1179–2022；其中 1,593 条能到日、1,260 条只到年、298 条无可用日期。
- **中国相关**：High Mountain Asia 表内 China 107 条（另有 Pakistan 159 / Kyrgyzstan 126 等）。
- **属性完整度**：洪水体积 820 条、湖泊体积 175 条、洪峰流量 762 条有值；
  机制列 1,831 条为空（源库只对部分事件判定机制）。
- **伤亡**：29 条有死亡数，合计 3,622 人。

## 已知限制（源数据自带，未改）

1. **事件库不是完整普查**：以文献/报告/新闻为线索，无人区与无记录事件缺席。
   冰岛与北美占了一半以上，反映的是**研究密度**而不是溃决频率。
2. **约 3% 无坐标**；影响与伤亡字段大部分缺失（未报 ≠ 0）。
3. **体积/流量多为估算**：源库另有 `VL_calculation` / `Qp_calculation` 口径字段，本目录未逐条附带。
4. **日期粒度不一**：只用 `Date` 列，`Date_Min`/`Date_Max` 区间原文保留在 attributes，
   不替源数据取中点。
5. **底层湖泊清单可能另有许可**：本目录只收编译后的库。

## 更新与校验

```bash
cd backend

uv run python -m scripts.external_data --check --dataset glof-db
uv run python -m scripts.external_data --refresh --dataset glof-db
# 离线复跑（用已下载的 ODS）
uv run python -m scripts.external_data --refresh --dataset glof-db --source /path/to/glofdatabase_V3-1.ods
```

读取栈：**python-calamine** 读 ODS 全部工作表 → 按位置映射短名 → **pandera（polars 后端）**
校验 ID 为正整数、坐标值域、伤亡非负；不通过就 `--refresh` 当场失败，不落盘。
