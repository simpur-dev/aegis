# public/terrain —— 自托管 quantized-mesh 地形（离线三维的必需资产之一）

页面按 `CesiumTerrainProvider.fromUrl('/terrain')` 取地形，**只连同源路径**，不使用任何托管地形服务。
缺这一份资产时页面仍可正常出图：`components/map/terrain.ts` 会退回椭球面
（决策函数 `chooseTerrainMode()` 已被 `terrain.spec.ts` 覆盖，界面上会显示"使用椭球面"的原因）。

## 需要烘焙成什么

```
public/terrain/
├── layer.json                  # 必需：元数据（tilejson/quantized-mesh-1.0）
└── {z}/{x}/{y}.terrain         # 必需：瓦片，未压缩后缀 .terrain；gzip 后为 .terrain.gz
```

* 投影/切片：EPSG:4326 地理坐标系（Cesium 的 `TiledGeoTerrain` 口径），`tilingScheme: "Geographic"`。
* 覆盖范围：西藏全域 `78°E–99°E, 26°N–37N`（与 `entities.TIBET_RECTANGLE`、后端 `persistence/geo.py` 同一口径）。
* 层级：`0–15` 足够现场判读（15 级约 150 m/像素；再深一层文件数翻 4 倍，收益有限）。
* 建议开启 `extensions: ["octvertexnormals"]`（配合 `requestVertexNormals: true`，否则法线由客户端估）。
* 应用探针：`HEAD /terrain/layer.json`。**探不到就用椭球面**，不会报错、不会白屏。

## 怎么生成（全程本地，无需联网到任何第三方瓦片服务）

原始数据用自备的 DEM GeoTIFF（如 SRTM 30m / Copernicus DEM，注意其许可与国界审图要求）。

```bash
# 方案一：Cesium Terrain Builder（开源，可自行 docker build，全程本地）
#         仓库：compass-analytics/cesium-terrain-builder
docker run --rm -v "$PWD:/data" ctb bash -c "\
  gdal_translate -of HTK /data/dem.tif /data/dem.dtm && \
  dtm2dem -b 26 78 37 99 /data/dem.dtm /data/dem/hm && \
  ctb-tile -f Mesh -C -o /data/terrain /data/dem/hm && \
  ctb-tile -f Mesh -l -o /data/terrain /data/dem/hm"
# 产物拷进 public/terrain/（layer.json + 各层 .terrain）

# 方案二：任何能产出 quantized-mesh-1.0 的本地编码器
#         只要求两点：*.terrain 字节流合法 + layer.json 与实际瓦片层级/范围一致
#         （layer.json 与瓦片不同源、混用两次烘焙结果，是这类资产最常见的翻车点）
```

烘焙自检（离线也生效）：

```bash
python -m http.server 8080 --directory public
curl -sI http://127.0.0.1:8080/terrain/layer.json | head -1   # 期望 200
```

## 常见现象

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 面板显示"未取到 /terrain/layer.json，使用椭球面" | 资产没拷进来或路径不对 | 按上面的目录摆放 |
| 显示"quantized-mesh 初始化失败…已回退椭球面" | layer.json 与瓦片不同源/层级不一致 | 重新生成，勿混用两次烘焙结果 |
| 贴地要素高度怪异 | 椭球面兜底时高程为 0 | 站点若台账有 `elevation_m`，前端已按绝对高度画 |
