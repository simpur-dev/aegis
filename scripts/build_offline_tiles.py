"""离线「一张图」烘焙资产生成器：quantized-mesh 地形 + PMTiles 栅格底图。

为什么要这个脚本：`terrain.ts`/`basemap.ts` 的决策与 provider 早就写好也有单测，
但仓库里从来没有一个真实瓦片字节——"弱网离线一张图"于是只被 mock 探针验证过，
格式假设（Hilbert tile id、varint 目录、quantized-mesh 的 zigzag-delta 与 high-water-mark 索引）
一次都没被真读取器读过。这里生成的每一个字节都必须能被仓库里那份真读取器吃下去，
所以字节布局逐字段对齐**随包安装的**实现，而不是对齐记忆或二手文档：

- PMTiles v3：`frontend/node_modules/pmtiles/src/index.ts`（`bytesToHeader`/`deserializeIndex`/
  `zxyToTileId`，4.5.0）；
- quantized-mesh：`frontend/node_modules/@cesium/engine/Source/Core/CesiumTerrainProvider.js`
  的 `createQuantizedMeshTerrainData`（1.145）。**这一版从 pos=0 直接读 center，
  没有 magic/version 前导**（全引擎搜 `0x024b` 无匹配），本脚本按它的口径写；
  `tests/unit/test_offline_tile_format.py` 里有一条源码漂移守卫，Cesium 改布局就会红。
- 索引/顶点编码：`AttributeCompression.zigZagDeltaDecode`（u/v/height 从 0 起累加）与
  Cesium 注释里的 Google high-water-mark（`indices[i] = highest - code`，code==0 则 highest++）。

资产性质（必须说清，别让人当成测绘数据）：**地形高程与底图像素都是解析函数生成的合成值，
不是任何真实 DEM/影像**。它们的作用是验证离线链路与给演示用；换真数据的方式见
`frontend/public/terrain/README.md` 与 `frontend/public/basemaps/README.md`。
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

# ---------- 常量 ----------

MAX_SHORT = 32767  # quantized-mesh 的顶点量化上限（Cesium 侧同名常量）
GRID = 32  # 每边顶点数（含边界），1024 个内部顶点
PNG_TILE_SIZE = 256
WGS84_A = 6378137.0
WGS84_B = 6356752.3142451793

# PMTiles v3 枚举（对齐 pmtiles/src/index.ts 的 Compression / TileType）
PMTILES_COMPRESSION_NONE = 1
PMTILES_COMPRESSION_GZIP = 2
PMTILES_TILE_TYPE_PNG = 2
PMTILES_HEADER_BYTES = 127
# 读取器一次性拿前 16384 字节并从里面切出根目录（getHeaderAndRoot），所以根目录必须落在这里面。
PMTILES_FIRST_CHUNK_BYTES = 16384
# 头部经纬度是 1e7 定点 int32，量化只能往框内缩：见 pmtiles_header 的边界那段。
PMTILES_COORD_FIXED_POINT = 1e7

# Web Mercator 的纬度上界（度）= atan(sinh(π))：切片方案把 y = ±(椭球半长轴·π) 米反投影就得到它。
# 烘全世界覆盖的影像归档时必须留在这个值以内，见 pmtiles_header 的注释。
WEB_MERCATOR_MAX_LATITUDE_DEG = math.atan(math.sinh(math.pi)) * 180.0 / math.pi

# Cesium extension id（QuantizedMeshExtensionIds.OCT_VERTEX_NORMALS）
EXTENSION_OCT_VERTEX_NORMALS = 1

PROVENANCE = "合成高程/合成影像：由 scripts/build_offline_tiles.py 的解析函数生成，不是真实 DEM 或卫星影像，仅用于离线链路验证与演示。"

# 默认输出目录按**脚本位置**锚定，不按进程工作目录：在 backend/ 下随手跑一次，
# 就会在 backend/frontend/public 里造出一套没人看的瓦（本项目已经在 Settings.env_file 上踩过同一类坑）。
DEFAULT_PUBLIC_DIR = Path(__file__).resolve().parent.parent / "frontend" / "public"


# ---------- 合成地表 ----------


def synthetic_height_m(lon_deg: float, lat_deg: float) -> float:
    """一个确定性、可复算的高程面（单位米）。

    形状刻意做成"青藏高原一带有起伏、其余地区接近 0"，因为它同时要喂地形与底图两条腿：
    两条腿用的是同一个函数，任何一侧改了都能对得上。
    """
    plateau = math.exp(-(((lon_deg - 88.0) / 12.0) ** 2 + ((lat_deg - 31.0) / 7.0) ** 2))
    ridge = 900.0 * math.sin(math.radians(lon_deg * 6.0)) * math.cos(math.radians(lat_deg * 5.0))
    swell = 1400.0 * math.sin(math.radians(lon_deg * 1.5 + 20.0)) * math.sin(math.radians(lat_deg * 2.0 - 10.0))
    base = 5200.0 * plateau
    return base + (ridge + swell) * plateau + 40.0 * plateau


def ecef(lon_deg: float, lat_deg: float, height_m: float) -> tuple[float, float, float]:
    """WGS84 大地坐标转地心直角坐标（ECEF）。"""
    lon = math.radians(lon_deg)
    lat = math.radians(lat_deg)
    sin_lat, cos_lat = math.sin(lat), math.cos(lat)
    e2 = 1.0 - (WGS84_B * WGS84_B) / (WGS84_A * WGS84_A)
    n = WGS84_A / math.sqrt(1.0 - e2 * sin_lat * sin_lat)
    x = (n + height_m) * cos_lat * math.cos(lon)
    y = (n + height_m) * cos_lat * math.sin(lon)
    z = (n * (1.0 - e2) + height_m) * sin_lat
    return x, y, z


@dataclass(frozen=True)
class GeographicRectangle:
    """GeographicTilingScheme（level 0 = 2×1，行 0 在北）里一瓦的经纬度范围。"""

    west: float
    south: float
    east: float
    north: float

    @property
    def center_lon(self) -> float:
        return (self.west + self.east) / 2.0

    @property
    def center_lat(self) -> float:
        return (self.south + self.north) / 2.0


def geographic_tile_rectangle(z: int, x: int, y: int) -> GeographicRectangle:
    x_tiles = 2 << z
    y_tiles = 1 << z
    width = 360.0 / x_tiles
    height = 180.0 / y_tiles
    west = -180.0 + x * width
    north = 90.0 - y * height
    return GeographicRectangle(west, north - height, west + width, north)


def geographic_tile_counts(z: int) -> tuple[int, int]:
    return 2 << z, 1 << z


@dataclass(frozen=True)
class Region:
    """按经纬度定义的一块关注区域（度）。地形加密只围绕它做。"""

    west: float
    south: float
    east: float
    north: float


# 与前端 `entities.ts` 的 TIBET_RECTANGLE 同一口径：一张图的默认视野就是受控区，
# 加密层级落在它身上才有意义（全球烘深是 62MB 量级的事，见 README 的取舍说明）。
DEFAULT_REFINE_REGION = Region(78.0, 26.0, 99.0, 37.0)


def region_tile_range(region: Region, z: int) -> tuple[int, int, int, int]:
    """层级 z 上与区域相交的瓦范围 `(x_start, x_end, y_start, y_end)`，闭区间。

    行号用 Cesium tiling scheme 的口径：**自北向南**（y=0 在最北）。`layer_json` 里写进
    `available` 时还要再翻一次，见 `tms_available_range`。
    """
    x_tiles, y_tiles = geographic_tile_counts(z)
    dx = 360.0 / x_tiles
    dy = 180.0 / y_tiles
    x_start = int((region.west + 180.0) // dx)
    x_end = int((region.east + 180.0) // dx)
    y_start = int((90.0 - region.north) // dy)
    y_end = int((90.0 - region.south) // dy)
    # 夹回有效范围：区域贴边（如 north=90）时向下取整会正好越界一格。
    return (
        max(0, min(x_start, x_tiles - 1)),
        max(0, min(x_end, x_tiles - 1)),
        max(0, min(y_start, y_tiles - 1)),
        max(0, min(y_end, y_tiles - 1)),
    )


def tms_available_range(region: Region, z: int) -> dict[str, int]:
    """把北向南的行号翻成 `layer.json` 的 `available` 口径。

    Cesium 读 `available` 时做的是 `yStart = yTiles - range.endY - 1 / yEnd = yTiles - range.startY - 1`
    （`_processLayerJsonTerrainProvider`，因为该字段沿用的是自南向北的 TMS 行号）。
    翻错的后果不是报错而是**可用性指向另一批瓦**：区域外的位置被判为"有更深瓦"，
    于是请求不存在的层级、区域里的位置被判为"没有"，于是永远停在浅层。
    """
    x_start, x_end, y_start, y_end = region_tile_range(region, z)
    _, y_tiles = geographic_tile_counts(z)
    return {"startX": x_start, "endX": x_end, "startY": y_tiles - 1 - y_end, "endY": y_tiles - 1 - y_start}


def availability_ranges(*, global_zoom: int, refine_zoom: int, region: Region) -> list[list[dict[str, int]]]:
    """逐层的 `available`：浅层是完整全球行，深层只声明区域那一段。

    层级数组必须从 0 连续排到 `refine_zoom`——Cesium 用它的长度当可用性深度，
    中间断一层就等于告诉引擎"下面没有了"。
    """
    levels: list[list[dict[str, int]]] = []
    for z in range(refine_zoom + 1):
        if z <= global_zoom:
            x_tiles, y_tiles = geographic_tile_counts(z)
            levels.append([{"startX": 0, "endX": x_tiles - 1, "startY": 0, "endY": y_tiles - 1}])
        else:
            levels.append([tms_available_range(region, z)])
    return levels


# ---------- 网格构造 ----------


@dataclass
class Mesh:
    """一瓦 quantized-mesh 的全部几何：顶点、三角形、四条边的索引与法向。"""

    rectangle: GeographicRectangle
    u: list[int]
    v: list[int]
    height: list[int]
    triangles: list[int]
    west_indices: list[int]
    south_indices: list[int]
    east_indices: list[int]
    north_indices: list[int]
    normals: list[tuple[float, float, float]]
    minimum_height: float
    maximum_height: float
    center: tuple[float, float, float]
    bounding_sphere_center: tuple[float, float, float]
    bounding_sphere_radius: float
    horizon_occlusion: tuple[float, float, float]


def _enu_basis(lon_deg: float, lat_deg: float) -> tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]:
    """瓦中心的局部东-北-天基（用来把解析坡度换成法向、把 ECEF 投到局部平面算包围球）。"""
    lon, lat = math.radians(lon_deg), math.radians(lat_deg)
    sin_lon, cos_lon = math.sin(lon), math.cos(lon)
    sin_lat, cos_lat = math.sin(lat), math.cos(lat)
    east = (-sin_lon, cos_lon, 0.0)
    north = (-sin_lat * cos_lon, -sin_lat * sin_lon, cos_lat)
    up = (cos_lat * cos_lon, cos_lat * sin_lon, sin_lat)
    return east, north, up


def build_mesh(rectangle: GeographicRectangle, *, skirt_height_m: float = 8.0) -> Mesh:
    """把经纬度矩形采成 GRID×GRID 网格，外加一圈裙边顶点。

    裙边顶点与边界顶点同参数、仅高程降低：目的是遮住相邻瓦之间的高度缝，
    代价是它是一面零厚度的墙——这不影响 Cesium 的解析与拼缝。
    """
    step_lon = (rectangle.east - rectangle.west) / (GRID - 1)
    step_lat = (rectangle.north - rectangle.south) / (GRID - 1)

    # 内部顶点：行 0 在北边（v=32767），与 GeographicTilingScheme 的 v 向上口径一致。
    grid_heights: list[list[float]] = []
    positions: list[tuple[float, float, float]] = []  # (u_norm, v_norm, height_m)
    for row in range(GRID):
        lat = rectangle.north - row * step_lat
        heights_row: list[float] = []
        for col in range(GRID):
            lon = rectangle.west + col * step_lon
            h = synthetic_height_m(lon, lat)
            heights_row.append(h)
            u_norm = col / (GRID - 1)
            v_norm = 1.0 - row / (GRID - 1)
            positions.append((u_norm, v_norm, h))
        grid_heights.append(heights_row)

    def grid_index(row: int, col: int) -> int:
        return row * GRID + col

    # 四条边各自的边界顶点（顺序 = 沿边前进方向），裙边按同样的顺序逐边追加。
    sides: dict[str, list[int]] = {
        "west": [grid_index(row, 0) for row in range(GRID)],
        "east": [grid_index(row, GRID - 1) for row in range(GRID)],
        "north": [grid_index(0, col) for col in range(GRID)],
        "south": [grid_index(GRID - 1, col) for col in range(GRID)],
    }
    skirt_base = GRID * GRID
    skirt_start: dict[str, int] = {}
    cursor = skirt_base
    for side in ("west", "south", "east", "north"):
        skirt_start[side] = cursor
        for vid in sides[side]:
            row, col = divmod(vid, GRID)
            h = grid_heights[row][col] - skirt_height_m
            u_norm = col / (GRID - 1)
            v_norm = 1.0 - row / (GRID - 1)
            positions.append((u_norm, v_norm, h))
            cursor += 1

    # 三角形：先按几何 id 建，再按"首次引用顺序"重编号——high-water-mark 编码要求
    # 新顶点必须正好等于当前 highest，重编号后这条天然成立。
    raw_triangles: list[tuple[int, int, int]] = []
    for row in range(GRID - 1):
        for col in range(GRID - 1):
            a = grid_index(row, col)
            b = grid_index(row, col + 1)
            c = grid_index(row + 1, col)
            d = grid_index(row + 1, col + 1)
            raw_triangles.append((a, b, c))
            raw_triangles.append((b, d, c))
    for side, ids in sides.items():
        base = skirt_start[side]
        for k in range(len(ids) - 1):
            current, nxt = ids[k], ids[k + 1]
            raw_triangles.append((current, nxt, base + k))
            raw_triangles.append((nxt, base + k + 1, base + k))

    remap: dict[int, int] = {}
    triangles: list[int] = []
    for tri in raw_triangles:
        for vid in tri:
            if vid not in remap:
                remap[vid] = len(remap)
        triangles.extend(remap[vid] for vid in tri)

    # 只保留被引用到的顶点（重编号后的顺序即引用顺序）。
    ordered_positions: list[tuple[float, float, float]] = [positions[vid] for vid in sorted(remap, key=lambda vid: remap[vid])]
    old_to_new_by_position = {vid: remap[vid] for vid in remap}

    heights = [p[2] for p in ordered_positions]
    min_h, max_h = min(heights), max(heights)
    span = (max_h - min_h) or 1.0
    quant = MAX_SHORT / span
    u_q = [max(0, min(MAX_SHORT, round(p[0] * MAX_SHORT))) for p in ordered_positions]
    v_q = [max(0, min(MAX_SHORT, round(p[1] * MAX_SHORT))) for p in ordered_positions]
    h_q = [max(0, min(MAX_SHORT, round((p[2] - min_h) * quant))) for p in ordered_positions]

    east, north, up = _enu_basis(rectangle.center_lon, rectangle.center_lat)
    normals = _surface_normals(ordered_positions, rectangle, east, north, up)

    def position_of(index: int) -> tuple[float, float, float]:
        lon = rectangle.west + ordered_positions[index][0] * (rectangle.east - rectangle.west)
        lat = rectangle.south + ordered_positions[index][1] * (rectangle.north - rectangle.south)
        return ecef(lon, lat, ordered_positions[index][2])

    cart = [position_of(i) for i in range(len(ordered_positions))]
    mid_height = (min_h + max_h) / 2.0
    center = ecef(rectangle.center_lon, rectangle.center_lat, mid_height)
    radius = max(math.dist(center, p) for p in cart)
    horizon = ecef(rectangle.center_lon, rectangle.center_lat, max_h)

    def remapped(vid: int) -> int:
        return old_to_new_by_position[vid]

    west_ids = sorted({remapped(grid_index(row, 0)) for row in range(GRID)})
    east_ids = sorted({remapped(grid_index(row, GRID - 1)) for row in range(GRID)})
    north_ids = sorted({remapped(grid_index(0, col)) for col in range(GRID)})
    south_ids = sorted({remapped(grid_index(GRID - 1, col)) for col in range(GRID)})

    return Mesh(
        rectangle=rectangle,
        u=u_q,
        v=v_q,
        height=h_q,
        triangles=triangles,
        west_indices=west_ids,
        south_indices=south_ids,
        east_indices=east_ids,
        north_indices=north_ids,
        normals=normals,
        minimum_height=min_h,
        maximum_height=max_h,
        center=center,
        bounding_sphere_center=center,
        bounding_sphere_radius=radius,
        horizon_occlusion=horizon,
    )


def _surface_normals(
    positions: list[tuple[float, float, float]],
    rectangle: GeographicRectangle,
    east: tuple[float, float, float],
    north: tuple[float, float, float],
    up: tuple[float, float, float],
) -> list[tuple[float, float, float]]:
    """逐顶点的解析坡度法向（先在瓦片中心 ENU 里求，再转到 ECEF 单位向量）。

    只影响 shading 与法向量存在性，不影响几何正确性；这里不做任何"真实地表"主张。
    """
    step = max(1e-9, (rectangle.east - rectangle.west) / (GRID - 1))
    meters_per_deg_lon = 111_320.0 * math.cos(math.radians(rectangle.center_lat))
    out: list[tuple[float, float, float]] = []
    for u_norm, v_norm, _h in positions:
        lon = rectangle.west + u_norm * (rectangle.east - rectangle.west)
        lat = rectangle.south + v_norm * (rectangle.north - rectangle.south)
        dh_dx = (synthetic_height_m(lon + step, lat) - synthetic_height_m(lon - step, lat)) / (2.0 * step * meters_per_deg_lon)
        dh_dy = (synthetic_height_m(lon, lat + step) - synthetic_height_m(lon, lat - step)) / (2.0 * step * 110_574.0)
        length = math.sqrt(dh_dx * dh_dx + dh_dy * dh_dy + 1.0)
        enu = (-dh_dx / length, -dh_dy / length, 1.0 / length)
        out.append(tuple(enu[0] * east[i] + enu[1] * north[i] + enu[2] * up[i] for i in range(3)))  # type: ignore[arg-type]
    return out


# ---------- 编码 ----------


def zigzag_encode(value: int) -> int:
    """对齐 Cesium `zigZagDecode(v) = (v >> 1) ^ -(v & 1)`。"""
    return (value << 1) ^ (value >> 31)


def delta_zigzag_encode(values: list[int]) -> list[int]:
    """从 0 起累加的 delta + zigzag（`zigZagDeltaDecode` 的反向）。"""
    out: list[int] = []
    previous = 0
    for value in values:
        out.append(zigzag_encode(value - previous))
        previous = value
    return out


def high_water_mark_encode(indices: list[int]) -> list[int]:
    """Cesium 注释里的 Google high-water-mark：decode 为 `v = highest - code`，code==0 则 highest++。

    顶点已按首次引用顺序重编号，因此这里必然编码得出去；编码不出去就是构造出了错。
    """
    codes: list[int] = []
    highest = 0
    for value in indices:
        if value == highest:
            codes.append(0)
            highest += 1
        elif value < highest:
            codes.append(highest - value)
        else:
            raise ValueError(f"索引 {value} 超前于 high-water mark {highest}：顶点没有按首次引用顺序编号")
    return codes


def oct_encode_normal(normal: tuple[float, float, float]) -> tuple[int, int]:
    """把单位向量八面体编码成两个 [-1,1] 再量化到 byte。"""
    x, y, z = normal
    sum_abs = abs(x) + abs(y) + abs(z)
    scale = 1.0 if sum_abs == 0.0 else 1.0 / sum_abs
    u = x * scale
    v = y * scale
    if z < 0.0:
        u = (1.0 - abs(v)) * (1.0 if u >= 0.0 else -1.0)
        v = (1.0 - abs(u)) * (1.0 if v >= 0.0 else -1.0)

    def to_byte(value: float) -> int:
        quantized = round(value * 127.0)
        return max(-127, min(127, quantized)) & 0xFF

    return to_byte(u), to_byte(v)


def encode_terrain_tile(mesh: Mesh) -> bytes:
    """写出 Cesium 1.145 `createQuantizedMeshTerrainData` 直接吃的字节。"""
    vertex_count = len(mesh.u)
    if vertex_count > 65536:
        raise ValueError("顶点数超过 u16 索引上限")

    parts: list[bytes] = []
    parts.append(struct.pack("<3d", *mesh.center))
    parts.append(struct.pack("<2f", mesh.minimum_height, mesh.maximum_height))
    parts.append(struct.pack("<4d", *mesh.bounding_sphere_center, mesh.bounding_sphere_radius))
    parts.append(struct.pack("<3d", *mesh.horizon_occlusion))

    parts.append(struct.pack("<I", vertex_count))
    encoded = delta_zigzag_encode(mesh.u) + delta_zigzag_encode(mesh.v) + delta_zigzag_encode(mesh.height)
    parts.append(struct.pack(f"<{len(encoded)}H", *encoded))
    assert len(parts[-1]) % 2 == 0, "索引缓冲区必须 2 字节对齐"

    codes = high_water_mark_encode(mesh.triangles)
    parts.append(struct.pack("<I", len(mesh.triangles) // 3))
    parts.append(struct.pack(f"<{len(codes)}H", *codes))

    for edge in (mesh.west_indices, mesh.south_indices, mesh.east_indices, mesh.north_indices):
        parts.append(struct.pack("<I", len(edge)))
        parts.append(struct.pack(f"<{len(edge)}H", *edge))

    normals = bytearray()
    for normal in mesh.normals[:vertex_count]:
        u_byte, v_byte = oct_encode_normal(normal)
        normals += bytes((u_byte, v_byte))
    parts.append(struct.pack("<B", EXTENSION_OCT_VERTEX_NORMALS))
    # layer.json 里声明 octvertexnormals（不是 vertexnormals）→ 小端 extensionLength。
    parts.append(struct.pack("<I", len(normals)))
    parts.append(bytes(normals))

    return b"".join(parts)


def layer_json(
    max_zoom: int,
    *,
    bounds: tuple[float, float, float, float],
    global_zoom: int,
    refine_region: Region = DEFAULT_REFINE_REGION,
) -> dict[str, object]:
    return {
        "format": "quantized-mesh-1.0",
        "version": "1.0",
        # slippyMap：URL 的 y 直接用 tiling scheme 的行号（Cesium 只对 tms 翻 y，见
        # CesiumTerrainProvider 里 terrainY 的分支）。注意 `available` 不跟着这个口径走：
        # 它始终是 TMS（自南向北），由 availability_ranges 负责翻。
        "scheme": "slippyMap",
        "projection": "EPSG:4326",
        "tiles": ["{z}/{x}/{y}.terrain"],
        "extensions": ["octvertexnormals"],
        "minzoom": 0,
        "maxzoom": max_zoom,
        "bounds": list(bounds),
        "attribution": PROVENANCE,
        "available": availability_ranges(global_zoom=global_zoom, refine_zoom=max_zoom, region=refine_region),
        "generator": "scripts/build_offline_tiles.py",
    }


# ---------- PNG 与底图 ----------


def png_bytes(pixels: list[tuple[int, int, int]]) -> bytes:
    """RGB8 PNG，无时间戳、无过滤：同样本必然同样字节。"""
    rows = bytearray()
    width = height = PNG_TILE_SIZE
    for y in range(height):
        rows.append(0)  # filter: None
        for x in range(width):
            rows += bytes(pixels[y * width + x])

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">2I5B", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + chunk(b"IEND", b"")


def hypsometric_rgb(height_m: float) -> tuple[int, int, int]:
    """合成高程 → 分层设色 + 简易山体阴影；刻意只用解析量，不引入任何外部色带文件。"""
    band = max(0.0, min(1.0, (height_m + 500.0) / 7000.0))
    r = int(235 - 150 * band)
    g = int(225 - 120 * band)
    b = int(200 - 160 * band)
    return r, g, b


def render_mercator_tile(z: int, x: int, y: int) -> bytes:
    """按 Web Mercator 瓦片渲染一帧合成底图：32×32 采样 + 双线性放大 + 经纬网。

    为什么不逐像素求值：纯 Python 下 256×256 每瓦要 6.5 万次三角函数，烘焙几十瓦就够慢；
    底图只是演示用的合成图，采样网格密度是可调的渲染参数，不是格式约束。
    """
    samples = 33
    n = 1 << z
    tile_west = x / n * 360.0 - 180.0
    tile_east = (x + 1) / n * 360.0 - 180.0

    def mercator_lat(world_row: float) -> float:
        """Web Mercator 行号（0 = 最北）→ 纬度。"""
        return math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * world_row / n))))

    grid: list[list[float]] = []
    for row in range(samples):
        lat = mercator_lat(y + row / (samples - 1))
        grid.append([synthetic_height_m(tile_west + (tile_east - tile_west) * col / (samples - 1), lat) for col in range(samples)])

    pixels: list[tuple[int, int, int]] = []
    step = (samples - 1) / PNG_TILE_SIZE
    for py in range(PNG_TILE_SIZE):
        gy = py * step
        y0, y1 = int(gy), min(samples - 1, int(gy) + 1)
        fy = gy - y0
        for px in range(PNG_TILE_SIZE):
            gx = px * step
            x0, x1 = int(gx), min(samples - 1, int(gx) + 1)
            fx = gx - x0
            h = grid[y0][x0] * (1 - fx) * (1 - fy) + grid[y0][x1] * fx * (1 - fy) + grid[y1][x0] * (1 - fx) * fy + grid[y1][x1] * fx * fy
            rgb = hypsometric_rgb(h)
            lon = tile_west + (tile_east - tile_west) * px / PNG_TILE_SIZE
            lat = mercator_lat(y + py / PNG_TILE_SIZE)
            if abs(lon % 10.0) < 360.0 / n / PNG_TILE_SIZE or abs(lat % 10.0) < 180.0 / n / PNG_TILE_SIZE:
                rgb = (120, 120, 140)  # 经纬网
            pixels.append(rgb)
    return png_bytes(pixels)


# ---------- PMTiles v3 ----------


def varint(value: int) -> bytes:
    """LEB128 无符号变长整数（pmtiles `readVarint` 的反向；本脚本规模用不到 5 字节路径）。"""
    if value < 0:
        raise ValueError("varint 不接负数")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _hilbert_rotate(n: int, x: int, y: int, rx: int, ry: int) -> tuple[int, int]:
    if ry == 0:
        return (n - 1 - y, n - 1 - x) if rx else (y, x)
    return x, y


def tile_id(z: int, x: int, y: int) -> int:
    """与 pmtiles `zxyToTileId` 逐行等价的 Hilbert 序号。"""
    if z > 26:
        raise ValueError("z 超过 26")
    if x >= (1 << z) or y >= (1 << z):
        raise ValueError("x/y 超出层级范围")
    acc = ((1 << z) * (1 << z) - 1) // 3
    tx, ty = x, y
    # JS 版用 `for (s = 1 << a; s > 0; s >>= 1)`，z=0 时 `1 << -1` 靠 JS 的移位回绕天然不进循环；
    # Python 的负移位会直接报错，所以这里显式按层数循环，语义与 z=0 不进入循环一致。
    for a in range(z - 1, -1, -1):
        s = 1 << a
        rx = tx & s
        ry = ty & s
        acc += ((3 * rx) ^ ry) * (1 << a)
        tx, ty = _hilbert_rotate(s, tx, ty, rx, ry)
    return acc


def encode_directory(entries: list[tuple[int, int, int, int]]) -> bytes:
    """(tile_id, offset, length, run_length) → pmtiles 的四段式 varint 目录。

    第四段偏移的口径来自 `deserializeIndex`：0 表示"接在前一条后面"，否则存 offset+1。
    """
    ordered = sorted(entries, key=lambda e: e[0])
    body = bytearray(varint(len(ordered)))
    last = 0
    for tile_id_value, _offset, _length, _run in ordered:
        body += varint(tile_id_value - last)
        last = tile_id_value
    for _tile_id_value, _offset, _length, run in ordered:
        body += varint(run)
    for _tile_id_value, _offset, length, _run in ordered:
        body += varint(length)
    previous_end = 0
    for index, (_tile_id_value, offset, length, _run) in enumerate(ordered):
        if index > 0 and offset == previous_end:
            body += varint(0)
        else:
            body += varint(offset + 1)
        previous_end = offset + length
    return bytes(body)


def pmtiles_header(
    *,
    root_offset: int,
    root_length: int,
    metadata_offset: int,
    metadata_length: int,
    data_offset: int,
    data_length: int,
    num_addressed: int,
    num_entries: int,
    num_contents: int,
    min_zoom: int,
    max_zoom: int,
    bounds: tuple[float, float, float, float],
    center: tuple[int, float, float],
) -> bytes:
    header = bytearray(127)
    header[0:7] = b"PMTiles"
    header[7] = 3
    struct.pack_into("<Q", header, 8, root_offset)
    struct.pack_into("<Q", header, 16, root_length)
    struct.pack_into("<Q", header, 24, metadata_offset)
    struct.pack_into("<Q", header, 32, metadata_length)
    struct.pack_into("<Q", header, 40, 0)  # leaf directory：本归档单层目录，无叶子
    struct.pack_into("<Q", header, 48, 0)
    struct.pack_into("<Q", header, 56, data_offset)
    struct.pack_into("<Q", header, 64, data_length)
    struct.pack_into("<Q", header, 72, num_addressed)
    struct.pack_into("<Q", header, 80, num_entries)
    struct.pack_into("<Q", header, 88, num_contents)
    header[96] = 1  # clustered：瓦片数据按 tile_id 递增落盘
    header[97] = PMTILES_COMPRESSION_NONE  # 目录不压缩
    header[98] = PMTILES_COMPRESSION_NONE  # PNG 自身已压缩，不再 gzip 一层
    header[99] = PMTILES_TILE_TYPE_PNG
    header[100] = min_zoom
    header[101] = max_zoom
    # 边界量化只准往框内缩（west/south 向上取整、east/north 向下取整）。四舍五入会出界：
    # round(85.05112877980659 * 1e7) = 850511288 → 读回 85.0511288，比 Web Mercator 的纬度上界
    # 还大 2e-8°，而 Cesium 要求影像 provider 的矩形被切片方案**完全包含**（ImageryLayer.js 注释原文），
    # 越界让 positionToTileXY 返回 undefined，`undefined.x` 直接把渲染循环打死（浏览器实测过）。
    west, south, east, north = bounds
    struct.pack_into("<i", header, 102, math.ceil(west * PMTILES_COORD_FIXED_POINT))
    struct.pack_into("<i", header, 106, math.ceil(south * PMTILES_COORD_FIXED_POINT))
    struct.pack_into("<i", header, 110, math.floor(east * PMTILES_COORD_FIXED_POINT))
    struct.pack_into("<i", header, 114, math.floor(north * PMTILES_COORD_FIXED_POINT))
    header[118] = center[0]
    struct.pack_into("<i", header, 119, round(center[1] * 1e7))
    struct.pack_into("<i", header, 123, round(center[2] * 1e7))
    return bytes(header)


def build_pmtiles(z_max: int, dest: Path) -> int:
    """写一个 PMTiles v3 归档，返回瓦片数。

    布局：header → 根目录 → JSON 元数据 → 瓦片数据。根目录紧跟 header，
    是因为读取器只从首个 16KB 切片里取根目录（见 PMTILES_FIRST_CHUNK_BYTES 注释）。
    """
    tiles: list[tuple[int, bytes]] = []
    for z in range(z_max + 1):
        for y in range(1 << z):
            for x in range(1 << z):
                tiles.append((tile_id(z, x, y), render_mercator_tile(z, x, y)))
    tiles.sort(key=lambda item: item[0])

    data = bytearray()
    entries: list[tuple[int, int, int, int]] = []
    for tile_id_value, payload in tiles:
        entries.append((tile_id_value, len(data), len(payload), 1))
        data += payload

    directory = encode_directory(entries)
    metadata = json.dumps(
        {
            "name": "aegis-offline-basemap",
            "type": "baselayer",
            "format": "pmtiles",
            "attribution": PROVENANCE,
            "generator": "scripts/build_offline_tiles.py",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    root_offset = PMTILES_HEADER_BYTES
    root_length = len(directory)
    metadata_offset = root_offset + root_length
    if metadata_offset + len(metadata) >= PMTILES_FIRST_CHUNK_BYTES:
        raise ValueError("根目录+元数据超出首个 16KB 块，读取器会切不到完整根目录")
    data_offset = metadata_offset + len(metadata)
    # 元数据段与瓦片段之间留 0 填充：让 tile_data_offset 落在 8 字节边界，Range 读更好对齐。
    data_offset += (-data_offset) % 8

    header = pmtiles_header(
        root_offset=root_offset,
        root_length=root_length,
        metadata_offset=metadata_offset,
        metadata_length=len(metadata),
        data_offset=data_offset,
        data_length=len(data),
        num_addressed=sum(1 << (2 * z) for z in range(z_max + 1)),
        num_entries=len(entries),
        num_contents=len(entries),
        min_zoom=0,
        max_zoom=z_max,
        bounds=(-180.0, -WEB_MERCATOR_MAX_LATITUDE_DEG, 180.0, WEB_MERCATOR_MAX_LATITUDE_DEG),
        center=(max(0, z_max - 1), 0.0, 0.0),
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(header + directory + metadata + b"\x00" * (data_offset - len(header) - root_length - len(metadata)) + bytes(data))
    return len(tiles)


# ---------- 烘焙编排 ----------


def bake(
    out_dir: Path,
    terrain_dir: Path,
    *,
    z_max: int,
    refine_z_max: int,
    refine_region: Region,
    pmtiles: bool,
    xyz: bool,
) -> dict[str, object]:
    out_dir.mkdir(parents=True, exist_ok=True)
    terrain_dir.mkdir(parents=True, exist_ok=True)

    tile_count = 0
    byte_total = 0
    per_level: dict[str, int] = {}
    for z in range(refine_z_max + 1):
        if z <= z_max:
            # 浅层铺满全球：Cesium 假定"层级 n 有瓦 ⇒ 0..n-1 的父瓦都在"，深一层的每一张
            # 都必须能回溯到烘过的父瓦，否则四叉树走到半路就没得下钻。
            x_tiles, y_tiles = geographic_tile_counts(z)
            tile_ids = [(x, y) for y in range(y_tiles) for x in range(x_tiles)]
        else:
            x_start, x_end, y_start, y_end = region_tile_range(refine_region, z)
            tile_ids = [(x, y) for y in range(y_start, y_end + 1) for x in range(x_start, x_end + 1)]
        for x, y in tile_ids:
            payload = encode_terrain_tile(build_mesh(geographic_tile_rectangle(z, x, y)))
            target = terrain_dir / str(z) / str(x)
            target.mkdir(parents=True, exist_ok=True)
            (target / f"{y}.terrain").write_bytes(payload)
            tile_count += 1
            byte_total += len(payload)
        per_level[str(z)] = len(tile_ids)
    (terrain_dir / "layer.json").write_text(
        json.dumps(
            layer_json(refine_z_max, bounds=(-180.0, -90.0, 180.0, 90.0), global_zoom=z_max, refine_region=refine_region),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    result: dict[str, object] = {
        "terrain_tiles": tile_count,
        "terrain_bytes": byte_total,
        "terrain_global_zoom": z_max,
        "terrain_refine_zoom": refine_z_max,
        "terrain_tiles_per_level": per_level,
    }
    if pmtiles:
        archive = out_dir / "aegis.pmtiles"
        if archive.exists():
            archive.unlink()
        result["pmtiles_tiles"] = build_pmtiles(z_max, archive)
        result["pmtiles_bytes"] = archive.stat().st_size
    if xyz:
        xyz_dir = out_dir / "xyz"
        if xyz_dir.exists():
            shutil.rmtree(xyz_dir)
        count = 0
        for z in range(z_max + 1):
            for y in range(1 << z):
                for x in range(1 << z):
                    target = xyz_dir / str(z) / str(x)
                    target.mkdir(parents=True, exist_ok=True)
                    target.joinpath(f"{y}.png").write_bytes(render_mercator_tile(z, x, y))
                    count += 1
        result["xyz_tiles"] = count
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="烘焙离线一张图资产（合成高程/合成影像，非真实测绘）")
    parser.add_argument("--public", type=Path, default=DEFAULT_PUBLIC_DIR, help="资产落盘目录（默认按脚本位置锚定的 frontend/public）")
    parser.add_argument("--max-zoom", type=int, default=2, help="全球铺满的最高层级（默认 2：提交进仓库的最小可用样本）")
    parser.add_argument(
        "--refine-max-zoom",
        type=int,
        default=None,
        help="受控区域内加密到的层级（默认 max-zoom+4）；决定引擎算出的裙边深度，见 README",
    )
    parser.add_argument(
        "--refine-region",
        type=float,
        nargs=4,
        metavar=("WEST", "SOUTH", "EAST", "NORTH"),
        default=None,
        help=f"加密区域（度），默认 {tuple(vars(DEFAULT_REFINE_REGION).values())}",
    )
    parser.add_argument("--no-pmtiles", action="store_true", help="只烘地形")
    parser.add_argument("--with-xyz", action="store_true", help="顺带烘一份 XYZ 模板金字塔（形态 A）")
    args = parser.parse_args(argv)

    if not (0 <= args.max_zoom <= 8):
        parser.error("--max-zoom 只允许 0..8（纯 Python 渲染，层级再高只是慢，不是不能用）")

    refine_zoom = args.max_zoom + 4 if args.refine_max_zoom is None else args.refine_max_zoom
    if refine_zoom < args.max_zoom:
        parser.error("--refine-max-zoom 不能低于 --max-zoom（那样等于没有加密层）")
    if not (0 <= refine_zoom <= 10):
        parser.error("--refine-max-zoom 只允许到 10：受控区外的瓦不烘，所以这个上限只约束区域内的张数")

    if args.refine_region is None:
        region = DEFAULT_REFINE_REGION
    else:
        west, south, east, north = args.refine_region
        if not (west < east and south < north):
            parser.error(f"--refine-region 的边界不成立：west={west} east={east} south={south} north={north}")
        region = Region(west, south, east, north)

    summary = bake(
        args.public / "basemaps",
        args.public / "terrain",
        z_max=args.max_zoom,
        refine_z_max=refine_zoom,
        refine_region=region,
        pmtiles=not args.no_pmtiles,
        xyz=args.with_xyz,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(PROVENANCE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
