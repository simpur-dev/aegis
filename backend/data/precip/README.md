# 历史降水数据

地图启动后从本目录读取日雨量和县域小时回放。两类历史数据都保留原始日期与缺测，单位和来源随图层显示。

| 文件 | 日期 | 内容 |
| --- | --- | --- |
| `map_rain/2020.json` 至 `map_rain/2024.json` | 2020–2024 | 从 CHM_PRE V2.1.1 裁出的吉隆 0.1° 格网，保留缺测，按年存放 |
| `station_daily.csv` | 2020–2022 | 三个县域代表点所在最近格点的逐日累计降水，单位 mm |
| `openmeteo/openmeteo_hourly.csv` | 2026-11-19 至 2026-11-20 UTC | Open-Meteo Historical Weather API 的小时累计降水，保留三个代表点的原始取样时间 |
| `stations.csv`、`stations.provenance.jsonl` | — | 县域代表点坐标、标识和来源，供地图定位与取样复现 |

逐日日期表示该日北京时间 20:00 结束的 24 小时累计。吉隆格网沿用原始间距，数值四舍五入到 0.01 mm。县域代表点统一用最近格点取样。

## 来源与许可

- **CHM_PRE V2.1.1** 由 Jinlong Hu、Chiyuan Miao、Qi Zhang 发布，原始数据及本项目的裁切和派生表按 CC BY 4.0 使用。 [Zenodo 原始数据与引用](https://doi.org/10.5281/zenodo.20378336) · [数据论文](https://doi.org/10.5194/essd-17-3987-2025) · [许可条款](https://creativecommons.org/licenses/by/4.0/)
- **Open-Meteo** 小时降水取自 Historical Weather API 的 `precipitation`，使用 UTC 时间并汇总为 CSV，数据许可为 CC BY 4.0。 [Open-Meteo 历史天气](https://open-meteo.com/en/docs/historical-weather-api) · [数据许可](https://open-meteo.com/en/terms)
- **县域代表点** 来自 Wikidata，按 CC0 1.0 发布。逐点来源见 `stations.provenance.jsonl`。 [Wikidata 数据许可](https://www.wikidata.org/wiki/Wikidata:Licensing)

## 重新生成吉隆格网

从仓库根目录运行下列命令。派生脚本需要后端 Python 环境的 `[data]` 扩展。

```bash
python backend/scripts/fetch_precip_chmpre.py --files daily --years 2020-2024 --channel zenodo
python backend/scripts/derive_map_rain.py
```
