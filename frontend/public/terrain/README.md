# public/terrain —— 自托管 quantized-mesh 地形（离线三维的必需资产之一）

页面按 `CesiumTerrainProvider.fromUrl('/terrain')` 取地形，**只连同源路径**，不使用任何托管地形服务。
缺这一份资产时页面仍可正常出图：`components/map/terrain.ts` 会退回椭球面
（决策函数 `chooseTerrainMode()` 已被 `terrain.spec.ts` 覆盖，界面上会显示"使用椭球面"的原因）。

## 仓库里现在带着什么（以及它不是什么）

```
layer.json + 0..2 层完整金字塔：42 张 .terrain，共 949,578 字节
```

这批瓦由 `python scripts/build_offline_tiles.py --max-zoom 2` 生成，**高程是解析函数算的合成值，
不是任何真实 DEM**（同一个函数也喂给底图那条腿，见 `basemaps/README.md`）。它的作用是让离线链路
在有真数据之前就能被真读取器验一遍——`offline-assets.spec.ts` 会真的 `HEAD layer.json`、真的取一张瓦、
真的按字节读；`backend/tests/unit/test_offline_tile_format.py` 里有一份按 Cesium 1.145 解析顺序镜像的
解码器逐字段读回每个字节，并带一条"上游解析顺序漂移即红"的守卫。

一个必须写下来的格式口径：这一版 Cesium 的 `createQuantizedMeshTerrainData` **从 pos=0 直接读 center，
不认 magic/version 前导**（本仓库的生成器与镜像解码器都按它写）。若换用别的地形服务端或旧版 Cesium，
先确认对方是否要求 quantized-mesh 规范里那 6 字节前导，别把"我们的瓦在自家页面能显示"当成"任何消费者都能读"。

要烘到更深层级（现场判读建议 0–15，覆盖西藏 `78°E–99°E, 26°N–37°N`）就直接加大 `--max-zoom`；
层级每深一级文件数乘 4，产物是否入库请自己决定——当前仓库只带 0–2 这一份最小可用样本。

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
