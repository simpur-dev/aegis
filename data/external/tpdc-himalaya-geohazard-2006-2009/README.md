# 喜马拉雅山地区重大地质灾害统计数据集（2006–2009）

> ⚠️ **许可：CC BY-NC-SA 4.0（非商业 · 相同方式共享）**。本目录与 AGPL-3.0 主仓库**隔离**，
> `manifest.json` 标 `commercial_use: false`；**商用发行前必须整目录移除或另行取得授权**。
> 演绎作品（含本目录的 `normalized/`）须沿用同一许可。详见
> [`LICENSE-CC-BY-NC-SA-4.0.md`](LICENSE-CC-BY-NC-SA-4.0.md)。

国家青藏高原科学数据中心（TPDC）发布的数据集，原始成果是
**《喜马拉雅山地区重大地质灾害遥感调查研究》**（童立强等著，科学出版社，2013）的数字化。

**这是目前西藏地质灾害点位质量最高的公开数据**：**525** 个调查点（泥石流 353 / 滑坡 153 /
溃决隐患冰湖 19），带**专业形态学属性**，覆盖喜马拉雅山全段（西起阿里札达、东至雅鲁藏布江为界）。

## 三个图层

| 图层 | 点数 | 灾种 | 关键属性 |
|---|---|---|---|
| 泥石流 | 353 | `debris_flow` | 规模、沟口高程、地形坡向、**流域形态**、**比降**、**发育阶段**、**水源类型** |
| 滑坡 | 153 | `landslide` | 前缘/后缘高程、**滑体坡度**、主滑方向、平面面积、**地层** |
| 溃决隐患冰川终碛湖 | 19 | `lake_outburst` | 湖面海拔、冰湖面积、**成灾风险**（重大/较大/…） |

这些属性正是平台因子引擎需要的输入形态：坡度、流域比降、发育阶段、水源类型、
冰湖成灾风险——**不是事件清单，是可直接当先验/易损性底数用的专业调查成果**。

## 文件

| 文件 | 内容 |
|---|---|
| `raw/tpdc_himalaya_geohazard.zip` | 原始交付 ZIP（98,842 字节）：3 组 shapefile（点 + DBF 属性）+ 1 个 xlsx（6 张表格） |
| `raw/tpdc_metadata.json` | TPDC 元数据接口原样返回（许可、DOI、时空范围、作者、useTerms） |
| `normalized/himalaya_geohazard_sites.v1.jsonl` | 525 条规范化记录 |
| `manifest.json` | 来源/许可/逐文件 SHA-256/行数/字段映射/已知限制 |
| `LICENSE-CC-BY-NC-SA-4.0.md` | 数据许可与商用限制 |

## 来源与署名

- 作者：**童立强（TONG Liqiang）等**
- 发布：国家青藏高原科学数据中心，2020-12-23
- DOI：`10.11888/Disas.tpdc.271000`；CSTR：`18406.11.Disas.tpdc.271000`
- 数据集页：<https://data.tpdc.ac.cn/en/data/a2128e3f-a913-40ba-a7b1-3318cb4031d5/>
- 抓取：TPDC 公开接口 `POST /file/file/batchDownloadFile?metadataId=…`（body `{"noToken":true}`），
  **无需登录**，脚本可复跑。

**引用**：

```text
童立强等. 喜马拉雅山地区重大地质灾害统计数据集[DS/OL].
国家青藏高原科学数据中心, 2020. https://doi.org/10.11888/Disas.tpdc.271000.
CSTR:18406.11.Disas.tpdc.271000. CC BY-NC-SA 4.0.

原始出版物：童立强等著. (2013). 喜马拉雅山地区重大地质灾害遥感调查研究[M]. 北京, 科学出版社.
```

## 数据摘要（本副本实测）

| 口径 | 数值 |
|---|---|
| 点位总数 | **525**（泥石流 353 / 滑坡 153 / 冰湖 19） |
| 经度范围 | 78.8695 — 95.3220 |
| 纬度范围 | 27.2033 — 31.0427（**另有 2 个越界点，见下**） |
| 坐标系 | WGS84（ESRI shapefile，`.prj` 写明 `GCS_WGS_1984`） |
| 调查时段 | 2006-01-01 — 2009-12-31 |
| 原始 ZIP | 98,842 字节 |

## 字段映射（DBF → normalized）

DBF 的数值字段是**科学计数法字符串**（如 `7.92296416667e+001` = 79.2296），直接 `float()`；
空值 → `null`（源未填，不是 0）。

| normalized 字段 | 源字段 | 说明 |
|---|---|---|
| `geo.longitude` / `geo.latitude` | shapefile 点几何 | WGS84 |
| `event.title` | `名称` / `冰湖名称` | |
| `event.location` | `所在地区` | 冰湖图层有，其余为空 |
| `hazard.type` | 图层名 | 泥石流→`debris_flow`、滑坡→`landslide`、冰湖→`lake_outburst` |
| `hazard.scale` | `规模` | 巨型/大型/中型… |
| `hazard.attributes.mouth_elevation_m` | `沟口_高程_` | 泥石流 |
| `hazard.attributes.channel_gradient` | `比降__` | 泥石流 |
| `hazard.attributes.development_stage` | `发育_阶段` | 泥石流（旺盛期/发育期/衰败期） |
| `hazard.attributes.toe_elevation_m` / `head_elevation_m` | `前缘高程` / `后缘高程` | 滑坡 |
| `hazard.attributes.slope_angle_deg` | `滑体坡度_` | 滑坡 |
| `hazard.attributes.aspect_deg` | `主滑方向_` | 滑坡（度） |
| `hazard.attributes.plan_area` | `平面面积_` | 滑坡 |
| `hazard.attributes.stratum` | `地层` | 滑坡（如 `J2n`） |
| `hazard.attributes.surface_elevation_m` / `surface_area` | `湖面海拔_m` / `冰湖面积_` | 冰湖 |
| `hazard.attributes.outburst_risk` | `成灾风险` | 冰湖（重大/…） |

## 已知问题（源数据自带，未改）

1. **2 个纬度明显异常**：`83.2012°`（扎锐清南）与 `81.5182°`（杂玛努）——西藏不可能。
   同一数据集的 xlsx 表格里也是这个值（`83°12'4.24"N`，首位数字笔误）。
   我们的口径：**不静默改源数据**，但给这两条打 `geo.quality_flags: ["latitude_out_of_range"]`，
   下游可据此筛掉。
2. **525 ≠ "540 余处"**：数据集说明的 540 含表格统计行与派生统计；本目录只收**有空间坐标的图层点**，
   差额不补造。
3. **是 2006–2009 年的成果**：不代表当前状况；新的滑坡/冰湖不在里面。
4. **只有点位与形态学属性，没有事件信息**：无发生日期、无伤亡、无损失——
   要做"历史灾情统计"得另配事件目录（NASA GLC / 公路普查）。
5. **冰湖只有 19 个**：这是"溃决隐患"的调查结论，不是全藏冰湖清单；
   全藏冰湖体量见另一数据集 `zenodo-parlung-zangbo-glacial-lakes`。

## 更新与校验

```bash
cd backend

# 校验：manifest 与盘上文件逐项一致（SHA-256 / 字节数 / 行数）
uv run python -m scripts.external_data --check --dataset tpdc-himalaya-geohazard-2006-2009

# 重抓（联网；无需登录）
uv run python -m scripts.external_data --refresh --dataset tpdc-himalaya-geohazard-2006-2009
```

改数据必须重跑 `--refresh` 并复核 manifest 的 diff；手改 `raw/` 或 `normalized/` 会被
`--check` 与 `tests/unit/test_external_data_catalog.py` 钉成红。
