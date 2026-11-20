# 全球泥石流规模目录（PANGAEA 783654）

> **许可：CC BY 3.0** —— 允许署名后的再分发与**商业使用**。
> 这是本目录里继 NASA GLC 之后第二条无 NC 限制的数据。

Riley, Bendick & Hyde (2012) 汇编的全球泥石流**规模目录**：988 条记录，每条给
**体积**（m³）、**堆积面积**（m²，部分缺失）、**是否火后**、**成因机制**、
文献出处与地点名。

## 它与 NASA GLC 的泥石流事件是什么关系

**两回事，分目录存放**：

| | 本目录 | `nasa-glc` |
|---|---|---|
| 性质 | 文献规模**编目** | 媒体报道的**事件** |
| 记录 | 988 条，一条一次泥石流规模估算 | 全球事件，带坐标/日期/伤亡 |
| 坐标 | **无**（只有地名） | 有（精度 1–50 km） |
| 时间 | 采样日期约一半为空 | 有事件日期 |

把"规模目录条目数"混进"事件数"会让下游统计失真，所以本目录 `source_category` 固定为
`debris_flow_magnitude_catalog`，**不参与**事件计数。

## 文件

| 文件 | 内容 |
|---|---|
| `raw/pangaea_783654_debris_flow_magnitude.tsv` | 原始 TSV（含 PANGAEA 注释头，按字节保留） |
| `normalized/global_debris_flow_magnitude.v1.jsonl` | 988 条规范化记录（统一信封） |
| `schema.json` | 单条记录的 JSON Schema（统一信封） |
| `manifest.json` | 来源/许可/逐文件 SHA-256/行数/读取栈/已知限制 |

## 来源与署名

- 作者：**Riley, Karin L; Bendick, Rebecca; Hyde, Kevin D**
- 发布：PANGAEA（2012）
- DOI：`10.1594/PANGAEA.783654`
- 数据文件：<https://doi.pangaea.de/10.1594/PANGAEA.783654?format=textfile>（匿名可下）
- 许可：CC BY 3.0（页面 `conditionsOfAccess=unrestricted`）

**引用**：

```text
Riley, Karin L; Bendick, Rebecca; Hyde, Kevin D (2012): Debris flow magnitudes:
a global catalog. PANGAEA. https://doi.org/10.1594/PANGAEA.783654. CC BY 3.0.
```

## 读取栈（2026-11-20 起）

**polars + pandera**：

- polars 先把 TSV 全列按字符串读入，再**显式 cast** —— 源文件数值列有空值，
  自动推断会把整列判成字符串或直接报错；
- pandera（polars 后端）校验列名/类型/值域/主键唯一（`Ord No` 1..988 不重复、
  体积与面积 ≥ 0）——校验不过就报错，不静默放行；
- 源文件有两列同名（`Area [m**2]` 与 `Area`），按**列位置**重命名后再校验，
  不按名字取（按名字会撞）。

## 字段映射

| 信封位置 | 源列 | 说明 |
|---|---|---|
| `record_id` | `Ord No` | `pangaea-debris-flow-magnitude:<序号>` |
| `source.event_id` | `Ord No` | |
| `event.date` | `Sampling date` | 尝试 `%B %d, %Y` 等格式；解析不了留 null |
| `event.date_raw` | `Sampling date` | 原文保留 |
| `event.title` / `event.location` | `Area`（地点列） | 源目录只有地名 |
| `event.notes` | `Fire event` | 关联野火 |
| `event.description` | `Comment` | |
| `hazard.type` | — | 常量 `debris_flow` |
| `hazard.source_category` | — | 常量 `debris_flow_magnitude_catalog` |
| `hazard.attributes.volume_m3` | `Vol [m**3]` | 全部有值 |
| `hazard.attributes.area_m2` | `Area [m**2]` | 860/988 为空 |
| `hazard.attributes.post_fire` | `Post-fire` | 原值：`yes`（264）/ `no`（663）/ `no evidence for fire…`（9）/ 空（52） |
| `hazard.attributes.post_fire_flag` | `Post-fire` | 仅 `yes`→true、`no`→false，其余 null（不替源数据推断） |
| `hazard.attributes.origin` | `Origin` | 成因机制（runoff-initiated / landslide-initiated…） |
| `hazard.attributes.method` | `Method comm` | 体积估算方法 |
| `hazard.attributes.reference` / `code` | `Reference` / `Code` | 文献出处与编码 |
| `geo.latitude` / `longitude` | — | **源目录没有坐标，留 null，不臆造** |
| `impact.*` | — | 源目录没有伤亡字段，留 null |

## 已知限制（源数据自带，未改）

1. **规模目录不是事件表**：无坐标、无伤亡、无逐条发生时间。
2. **采样日期约一半为空**（517/988）。
3. **面积缺失 860/988**：源目录只对部分事件估算了堆积面积。
4. **`Post-fire` 有非二值取值**：9 条是 `no evidence for fire (charcoal not found…)`，
   52 条为空——按原值保留，`post_fire_flag` 只对 yes/no 给布尔。
5. **与 GLC 事件可能重叠**，但口径不同（文献规模汇编 vs 媒体报道事件），本目录不做去重合并。

## 更新与校验

```bash
cd backend

uv run python -m scripts.external_data --check --dataset pangaea-debris-flow-magnitude
uv run python -m scripts.external_data --refresh --dataset pangaea-debris-flow-magnitude
# 离线复跑（用已下载的源 TSV）
uv run python -m scripts.external_data --refresh --dataset pangaea-debris-flow-magnitude --source /path/to/rows.tsv
```
