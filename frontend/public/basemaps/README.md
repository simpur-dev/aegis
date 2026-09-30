# public/basemaps —— 离线底图与本区地理底册（「一张图」的同源数据源）

页面按 `components/map/basemap.ts` 的降级链取底图，**只连同源路径**：

```
PMTiles 单文件  →  XYZ 瓦片金字塔  →  不挂影像（只画地形 + 矢量要素）
```

三条路都不通也只是"没有影像"，不会白屏（`chooseBasemapSource()` 返回 `kind:'none'`，
Viewer 收到 `baseLayer: false`，地球用深色底色）。任何指向非同源的地址都会在
`offline.isLocalAssetUrl()` 这一步被拒（`basemap.spec.ts` 与 `guard.spec.ts` 钉住了这条约束）。

## 1) 底图：两种可烘焙形态

### 形态 A（推荐）：栅格 PMTiles 单文件

```
public/basemaps/aegis.pmtiles
```

* 格式：**PMTiles v3**，`TileType` 必须是**栅格**（PNG=2 / JPEG=3 / WebP=4 / AVIF=5）。
  矢量 MVT(=1) 不能当影像用，代码会直接拒收并提示走形态 C；
* 切片方案：EPSG:3857（`WebMercatorTilingScheme`），`minZoom=0`，`maxZoom` 建议 12–14；
* 边界：可写西藏范围；写成全 0 会被当作全世界（`describePmtilesHead()` 的退化分支）；
* 应用探针：`HEAD /basemaps/aegis.pmtiles`。运行时只按 Range 取需要的字节，弱网友好。

生成（本地，不连任何第三方瓦片服务）：

```bash
# 1. 先用自备数据出目录金字塔：planetiler（矢量）不适用，这里要栅格
#    常用组合：Natural Earth / 自采影像 + GDAL
gdal2tiles.py -z 0-12 -w none --xyz -t none -g none dem_or_rgb.tif tiles_png

# 2. 目录 → PMTiles（go-pmtiles 是纯本地工具）
go-pmtiles directory-to-pmtiles tiles_png aegis.pmtiles

# 3. 校验（不需要网络）
go-pmtiles overview aegis.pmtiles
```

### 形态 B：XYZ 瓦片目录金字塔

```
public/basemaps/{z}/{x}/{y}.png          # 默认模板，见 MAP_ASSET_PATHS.basemapTemplate
```

* 与形态 A 的第 1 步产物完全一致，直接拷进来即可（不转 PMTiles 也能跑）；
* 缺点：数万个小文件，拷盘/校验慢；优点：最直白，任何静态服务器都能供。

### 形态 C：同源瓦片服务（矢量底图的正路）

矢量 PMTiles 想要好看的中文注记与分级符号，就让 **Martin / Tegola / tileserver-gl** 在同一站点下反代出
`/tiles/{z}/{x}/{y}.png`（或让网关出 PNG 栅格），然后把模板传进页面即可（改 `MapView.vue` 里
`createMapScene` 的 `basemap.template`，或后端补一个配置接口）。仍然是同源路径，不算外链。

## 2) `region-anchors.json` —— 区划锚点（决定预警/站点能不能落到图上）

后端 `TelemetryReading` 与 `WarningRecord` **只有 `region_code`，没有经纬度**（`api/app.py` 无带坐标的路由）。
所以面要素与预警 marker 的落点只能靠这份离线底册；缺它时相关要素全部进「未定位清单」。

```json
{
  "items": [
    { "code": "540121", "name_zh": "林周县", "lon": 91.287, "lat": 30.041 },
    { "code": "540221", "name_zh": "南木林县", "lon": 89.066, "lat": 29.632 }
  ]
}
```

* `code`：与后端一致的 6 位县级以上行政区划代码（`region_code` 的取值，如模拟器用的 `540121/540221/540321`）；
* `lon/lat`：区县政府驻地（WGS84，十进制度，经度在前）。**必须自备合规数据**（天地图/民政公布区划/自采底册），
  本仓库刻意不预置任何坐标，避免把未核对的位置当成真值；
* 顶层也接受裸数组；形状不合规的条目会被 `normalizeAnchors()` 逐条丢弃；
* 想按面而不是按点画行政区划：把它们烘成 GeoJSON 走下一节的灾害面通道，或另开一个面图层。

## 3) `hazard-zones.geojson` —— 灾害分区面（贴地渲染，可点选）

```json
{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "properties": { "id": "zone-540121-01", "name": "擦巴拉沟泥石流重点区",
                       "region_code": "540121", "risk_level": 2, "hazard_type": "debris_flow" },
      "geometry": { "type": "Polygon", "coordinates": [[[91.10,30.02],[91.20,30.02],[91.20,30.12],[91.10,30.12],[91.10,30.02]]] }
    }
  ]
}
```

* 几何只支持 `Polygon` / `MultiPolygon`（MultiPolygon 会拆成多个可点选面，id 追加 `#序号`）；
* `properties` 可缺 `risk_level`/`hazard_type`：缺省时按 `region_code` 用链路评估（`/api/v1/events` 的 `risk`）兜底；
* 坐标必须是 `[lon, lat]` 且**不能是 (0,0)**：坏几何不静默丢，会进面板的「未渲染的几何」列表并写明理由
  （`buildHazardZoneLayer()` 的 `rejected[]`，已在 `entities.spec.ts` 覆盖）；
* 应用探针：`HEAD /basemaps/hazard-zones.geojson`，取不到就是空面层。

## 4) 配套：地图引擎的静态资源（一次性拷贝，不是可选项）

引擎需要 Workers / Assets / ThirdParty / Widgets 四类静态目录，打包后无法从 `node_modules` 自动带入：

```bash
mkdir -p public/cesium
cp -r node_modules/cesium/Build/Cesium/{Workers,Assets,ThirdParty,Widgets} public/cesium/
```

`viewer.ts` 在建场前会把 `window.CESIUM_BASE_URL` 指到 `<BASE_URL>cesium/`（已设置则不覆盖）。
若希望这步自动化，允许我改 `vite.config.ts` 的话，加 `vite-plugin-static-copy`（或
`vite-plugin-cesium`）把上述目录在构建期拷进 `dist/cesium/` 即可 —— 见交付报告里的"需要你接线"清单。

## 5) 离线验收（现场合上网络也要过）

```bash
npm run build && npm run preview -- --port 4173
# 关掉 Wi-Fi 后：
#   1. /map 能出图（影像来自 PMTiles 或 XYZ；地形没烘则面板显示"使用椭球面"）
#   2. 浏览器 Network 面板里除 127.0.0.1 外零请求
#   3. 右上状态标签为「离线/不可达」或「降级运行」，而不是崩溃
```
