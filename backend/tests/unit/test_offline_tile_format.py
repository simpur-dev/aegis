"""离线一张图资产格式：解码自证 + 上游读取器源码漂移守卫。

为什么不只测"文件存在"：`terrain.ts`/`basemap.ts` 的探针只证明文件在，不证明字节能被
Cesium/pmtiles 解析——这正是不接真资产就永远发现不了的那类缺陷。这里用一份**按上游
读取器逐字段镜像**的解码器把每个字节读回去，并加一条守卫：一旦仓库里安装的 Cesium 或
pmtiles 改了字段偏移/枚举，镜像解码就会失去依据，测试当场红，而不是等到浏览器里白屏。

资产本身是合成高程/合成影像（见 PROVENANCE），这里验的是格式与可解析性，不是测绘精度。
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import math
import struct
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]  # backend/tests/unit/... → 仓库根
SCRIPT_PATH = REPO_ROOT / "scripts" / "build_offline_tiles.py"
PUBLIC_DIR = REPO_ROOT / "frontend" / "public"
CESIUM_SOURCE = REPO_ROOT / "frontend" / "node_modules" / "@cesium" / "engine" / "Source" / "Core" / "CesiumTerrainProvider.js"
PMTILES_SOURCE = REPO_ROOT / "frontend" / "node_modules" / "pmtiles" / "src" / "index.ts"

MAX_SHORT = 32767
PMTILES_HEADER_BYTES = 127


def load_builder():
    assert SCRIPT_PATH.is_file(), f"烘焙脚本缺失：{SCRIPT_PATH}"
    spec = importlib.util.spec_from_file_location("build_offline_tiles", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # @dataclass 在求值时要按 __module__ 找回命名空间；不先登记进 sys.modules 就会炸在
    # dataclasses 内部（'NoneType' object has no attribute '__dict__'），而不是炸在脚本里。
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


BUILDER = load_builder()


# ---------- quantized-mesh 解码（镜像 CesiumTerrainProvider.createQuantizedMeshTerrainData） ----------


def _zigzag_decode(value: int) -> int:
    return (value >> 1) ^ -(value & 1)


def decode_terrain_tile(payload: bytes) -> dict[str, object]:
    view_offsets = 0
    center = struct.unpack_from("<3d", payload, view_offsets)
    view_offsets += 24
    min_height, max_height = struct.unpack_from("<2f", payload, view_offsets)
    view_offsets += 8
    sphere_center = struct.unpack_from("<3d", payload, view_offsets)
    view_offsets += 24
    sphere_radius = struct.unpack_from("<d", payload, view_offsets)[0]
    view_offsets += 8
    horizon = struct.unpack_from("<3d", payload, view_offsets)
    view_offsets += 24

    (vertex_count,) = struct.unpack_from("<I", payload, view_offsets)
    view_offsets += 4
    raw_vertices = struct.unpack_from(f"<{vertex_count * 3}H", payload, view_offsets)
    view_offsets += vertex_count * 6
    assert view_offsets % 2 == 0, "索引缓冲区必须 2 字节对齐"

    u_values: list[int] = []
    v_values: list[int] = []
    h_values: list[int] = []
    cursor_u = cursor_v = cursor_h = 0
    for i in range(vertex_count):
        cursor_u += _zigzag_decode(raw_vertices[i])
        cursor_v += _zigzag_decode(raw_vertices[vertex_count + i])
        cursor_h += _zigzag_decode(raw_vertices[2 * vertex_count + i])
        u_values.append(cursor_u)
        v_values.append(cursor_v)
        h_values.append(cursor_h)

    (triangle_count,) = struct.unpack_from("<I", payload, view_offsets)
    view_offsets += 4
    codes = struct.unpack_from(f"<{triangle_count * 3}H", payload, view_offsets)
    view_offsets += triangle_count * 6
    indices: list[int] = []
    highest = 0
    for code in codes:
        value = highest - code
        indices.append(value)
        if code == 0:
            highest += 1
    edge_lists: dict[str, list[int]] = {}
    for name in ("west", "south", "east", "north"):
        (edge_count,) = struct.unpack_from("<I", payload, view_offsets)
        view_offsets += 4
        edge_lists[name] = list(struct.unpack_from(f"<{edge_count}H", payload, view_offsets))
        view_offsets += edge_count * 2

    extensions: list[tuple[int, bytes]] = []
    while view_offsets < len(payload):
        (extension_id,) = struct.unpack_from("<B", payload, view_offsets)
        view_offsets += 1
        (extension_length,) = struct.unpack_from("<I", payload, view_offsets)  # octvertexnormals → 小端
        view_offsets += 4
        body = payload[view_offsets : view_offsets + extension_length]
        assert len(body) == extension_length, "扩展长度超出缓冲区：声明与实写字节不符"
        view_offsets += extension_length
        extensions.append((extension_id, body))

    return {
        "center": center,
        "minimum_height": min_height,
        "maximum_height": max_height,
        "bounding_sphere_center": sphere_center,
        "bounding_sphere_radius": sphere_radius,
        "horizon_occlusion": horizon,
        "vertex_count": vertex_count,
        "u": u_values,
        "v": v_values,
        "height": h_values,
        "triangles": indices,
        "edges": edge_lists,
        "extensions": extensions,
        "consumed_bytes": view_offsets,
    }


def baked_terrain_tiles() -> list[Path]:
    """瓦片按 `{z}/{x}/{y}.terrain` 落盘，所以要三层通配——写成两层会得到空列表并让断言空转。"""
    return sorted(PUBLIC_DIR.joinpath("terrain").glob("*/*/*.terrain"))


# ---------- 断言 ----------


def test_baked_assets_exist_and_are_not_placeholders() -> None:
    assert baked_terrain_tiles(), "public/terrain 下没有任何 .terrain 瓦片"
    assert PUBLIC_DIR.joinpath("basemaps", "aegis.pmtiles").is_file(), "public/basemaps/aegis.pmtiles 缺失"


def test_layer_json_matches_files_on_disk() -> None:
    layer = json.loads((PUBLIC_DIR / "terrain" / "layer.json").read_text(encoding="utf-8"))
    assert layer["format"] == "quantized-mesh-1.0"
    assert layer["scheme"] == "slippyMap", "scheme 决定 URL 的 y 是否翻面，必须与烘焙口径一致"
    assert layer["projection"] == "EPSG:4326"
    assert "octvertexnormals" in layer["extensions"]
    max_zoom = layer["maxzoom"]
    for z, ranges in enumerate(layer["available"]):
        x_tiles, y_tiles = BUILDER.geographic_tile_counts(z)
        assert ranges == [{"startX": 0, "endX": x_tiles - 1, "startY": 0, "endY": y_tiles - 1}]
    for path in baked_terrain_tiles():
        z = int(path.parent.parent.name)
        x = int(path.parent.name)
        y = int(path.stem)
        assert 0 <= z <= max_zoom
        assert 0 <= x < BUILDER.geographic_tile_counts(z)[0]
        assert 0 <= y < BUILDER.geographic_tile_counts(z)[1]


def test_provenance_is_carried_by_both_asset_entries() -> None:
    """和内置案例库同一口径：合成资产必须自带"这不是测绘数据"的说明，且不能被截断。"""
    layer = json.loads((PUBLIC_DIR / "terrain" / "layer.json").read_text(encoding="utf-8"))
    assert "合成" in layer["attribution"]
    assert "不是真实" in layer["attribution"]
    assert "合成" in layer["generator"] or layer["generator"].endswith("build_offline_tiles.py")


def test_every_terrain_tile_decodes_and_is_geometrically_consistent() -> None:
    for path in baked_terrain_tiles():
        decoded = decode_terrain_tile(path.read_bytes())
        vertex_count = decoded["vertex_count"]
        assert vertex_count >= BUILDER.GRID * BUILDER.GRID
        assert decoded["consumed_bytes"] == path.stat().st_size, f"{path}：字节没读完，读到的布局与实际写入不一致"
        assert decoded["minimum_height"] <= decoded["maximum_height"]
        assert decoded["bounding_sphere_radius"] > 0.0
        for name, values in (("u", decoded["u"]), ("v", decoded["v"]), ("height", decoded["height"])):
            assert all(0 <= value <= MAX_SHORT for value in values), f"{path}: {name} 量化越界"
        assert max(decoded["u"]) == MAX_SHORT and min(decoded["u"]) == 0, f"{path}: u 没铺满 [0,32767]"
        assert all(index < vertex_count for index in decoded["triangles"]), f"{path}: 索引指向不存在的顶点"
        assert len(decoded["triangles"]) % 3 == 0
        for name, edge in decoded["edges"].items():
            assert edge, f"{path}: {name} 边为空，相邻瓦无法拼缝"
            assert all(index < vertex_count for index in edge), f"{path}: {name} 边索引越界"
        assert decoded["extensions"], f"{path}: 没有 octvertexnormals 扩展，而 requestVertexNormals 是 true"
        extension_id, body = decoded["extensions"][0]
        assert extension_id == BUILDER.EXTENSION_OCT_VERTEX_NORMALS
        assert len(body) == 2 * vertex_count, f"{path}: 法向字节数应为每顶点 2 字节"


def test_encode_decode_round_trip_is_lossless() -> None:
    """量化发生在编码之前，所以解码回来的整数数组必须逐值相等——包括索引与边。"""
    mesh = BUILDER.build_mesh(BUILDER.geographic_tile_rectangle(1, 2, 1))
    decoded = decode_terrain_tile(BUILDER.encode_terrain_tile(mesh))
    assert decoded["vertex_count"] == len(mesh.u)
    assert decoded["u"] == mesh.u
    assert decoded["v"] == mesh.v
    assert decoded["height"] == mesh.height
    assert decoded["triangles"] == mesh.triangles
    for name in ("west", "south", "east", "north"):
        assert decoded["edges"][name] == getattr(mesh, f"{name}_indices")
    assert abs(decoded["minimum_height"] - mesh.minimum_height) < 1e-3
    assert abs(decoded["maximum_height"] - mesh.maximum_height) < 1e-3


def test_baked_tile_heights_agree_with_the_surface_function() -> None:
    """内部顶点必须贴在解析高程面上；裙边顶点按定义低一整段裙高，两者分开断言。

    容差不随手写米数：内部顶点的偏差来自 u/v 量化（最多挪动一个格点）与高程量化，
    所以用该瓦自身的最大格点高差来定界。
    """
    rectangle = BUILDER.geographic_tile_rectangle(2, 5, 1)  # 落在高原腹地的瓦，高差有实质量
    skirt_height_m = inspect.signature(BUILDER.build_mesh).parameters["skirt_height_m"].default
    decoded = decode_terrain_tile((PUBLIC_DIR / "terrain" / "2" / "5" / "1.terrain").read_bytes())
    lon_step = (rectangle.east - rectangle.west) / (BUILDER.GRID - 1)
    lat_step = (rectangle.north - rectangle.south) / (BUILDER.GRID - 1)
    gradient = 0.0
    for row in range(BUILDER.GRID):
        for col in range(BUILDER.GRID):
            lon = rectangle.west + col * lon_step
            lat = rectangle.south + row * lat_step
            if col + 1 < BUILDER.GRID:
                gradient = max(gradient, abs(BUILDER.synthetic_height_m(lon + lon_step, lat) - BUILDER.synthetic_height_m(lon, lat)))
            if row + 1 < BUILDER.GRID:
                gradient = max(gradient, abs(BUILDER.synthetic_height_m(lon, lat + lat_step) - BUILDER.synthetic_height_m(lon, lat)))

    span = decoded["maximum_height"] - decoded["minimum_height"]
    assert span > 10 * skirt_height_m, "整幅高差太小，下面的容差推导没有意义"
    quantization_slack = span / MAX_SHORT
    interior_worst = 0.0
    interior_checked = 0
    for index in range(decoded["vertex_count"]):
        u, v = decoded["u"][index], decoded["v"][index]
        if u in (0, MAX_SHORT) or v in (0, MAX_SHORT):
            continue  # 边界顶点带裙高，另作断言
        lon = rectangle.west + u / MAX_SHORT * (rectangle.east - rectangle.west)
        lat = rectangle.south + v / MAX_SHORT * (rectangle.north - rectangle.south)
        actual = decoded["minimum_height"] + decoded["height"][index] / MAX_SHORT * span
        interior_worst = max(interior_worst, abs(actual - BUILDER.synthetic_height_m(lon, lat)))
        interior_checked += 1
    assert interior_checked > 500, "内部顶点取样太少，这条断言几乎没在跑"
    assert interior_worst <= 2.0 * gradient + quantization_slack, (
        f"内部顶点最大偏差 {interior_worst:.1f}m 超出由格点梯度推出的容差 {2.0 * gradient + quantization_slack:.1f}m"
    )

    # 裙边：最低顶点应当正好是"某条边界上的地表值 − 裙高"，偏差只留量化余量。
    surface_min = min(
        BUILDER.synthetic_height_m(rectangle.west + col * lon_step, rectangle.south + row * lat_step)
        for row in range(BUILDER.GRID)
        for col in range(BUILDER.GRID)
    )
    assert abs(decoded["minimum_height"] - (surface_min - skirt_height_m)) <= quantization_slack + 1e-3
    assert decoded["maximum_height"] <= surface_min + span


def test_encoding_helpers_are_invertible() -> None:
    values = [0, 1, 32767, 30000, 32767, 512]
    encoded = BUILDER.delta_zigzag_encode(values)
    decoded: list[int] = []
    cursor = 0
    for code in encoded:
        cursor += _zigzag_decode(code)
        decoded.append(cursor)
    assert decoded == values

    # high-water-mark：只有"首次引用顺序"的索引才编得出去，反解必须原样回来。
    indices = [0, 1, 2, 1, 3, 2, 0]
    codes = BUILDER.high_water_mark_encode(indices)
    out: list[int] = []
    highest = 0
    for code in codes:
        value = highest - code
        out.append(value)
        if code == 0:
            highest += 1
    assert out == indices

    with pytest.raises(ValueError, match="high-water mark"):
        BUILDER.high_water_mark_encode([0, 5])


def test_oct_encoded_normals_are_unit_length() -> None:
    mesh = BUILDER.build_mesh(BUILDER.geographic_tile_rectangle(1, 1, 0))
    for normal in mesh.normals[:64]:
        assert abs(math.dist(normal, (0.0, 0.0, 0.0)) - 1.0) < 1e-6
        packed = BUILDER.oct_encode_normal(normal)
        assert all(0 <= byte <= 255 for byte in packed)


def test_hilbert_tile_ids_cover_expected_sequence() -> None:
    """层级 0..2 的 Hilbert 序号必须连续不重复，且从每层起点累加（(4^z-1)/3）。"""
    ids = [BUILDER.tile_id(z, x, y) for z in range(3) for y in range(1 << z) for x in range(1 << z)]
    assert len(ids) == len(set(ids)) == 21
    assert ids[0] == 0
    assert BUILDER.tile_id(1, 0, 0) == 1
    assert BUILDER.tile_id(2, 0, 0) == 5
    with pytest.raises(ValueError, match="26"):
        BUILDER.tile_id(27, 0, 0)
    with pytest.raises(ValueError, match="超出"):
        BUILDER.tile_id(1, 2, 0)


def test_varint_matches_leb128_widths() -> None:
    assert BUILDER.varint(0) == b"\x00"
    assert BUILDER.varint(127) == b"\x7f"
    assert BUILDER.varint(128) == b"\x80\x01"
    assert BUILDER.varint(300) == b"\xac\x02"
    with pytest.raises(ValueError, match="负数"):
        BUILDER.varint(-1)


# ---------- PMTiles：用 Python 侧镜像读回真实字节 ----------


def _read_varint(payload: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        byte = payload[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def decode_pmtiles(payload: bytes) -> dict[str, object]:
    assert payload[0:7] == b"PMTiles", "魔数不是 PMTiles"
    assert payload[7] == 3, "spec version 必须是 3"

    def field(offset: int) -> int:
        return int.from_bytes(payload[offset : offset + 8], "little")

    header = {
        "rootDirectoryOffset": field(8),
        "rootDirectoryLength": field(16),
        "jsonMetadataOffset": field(24),
        "jsonMetadataLength": field(32),
        "tileDataOffset": field(56),
        "tileDataLength": field(64),
        "numAddressedTiles": field(72),
        "numTileEntries": field(80),
        "numTileContents": field(88),
        "clustered": payload[96] == 1,
        "internalCompression": payload[97],
        "tileCompression": payload[98],
        "tileType": payload[99],
        "minZoom": payload[100],
        "maxZoom": payload[101],
        "minLon": int.from_bytes(payload[102:106], "little", signed=True) / 1e7,
        "minLat": int.from_bytes(payload[106:110], "little", signed=True) / 1e7,
        "maxLon": int.from_bytes(payload[110:114], "little", signed=True) / 1e7,
        "maxLat": int.from_bytes(payload[114:118], "little", signed=True) / 1e7,
    }
    root_offset = header["rootDirectoryOffset"]
    root_length = header["rootDirectoryLength"]
    assert root_offset + root_length <= PMTILES_FIRST_CHUNK, "读取器只从首个 16KB 切片里切根目录，超出的归档会被切成半截"
    directory = payload[root_offset : root_offset + root_length]
    num_entries, pos = _read_varint(directory, 0)
    tile_ids: list[int] = []
    last = 0
    for _ in range(num_entries):
        delta, pos = _read_varint(directory, pos)
        last += delta
        tile_ids.append(last)
    run_lengths: list[int] = []
    for _ in range(num_entries):
        value, pos = _read_varint(directory, pos)
        run_lengths.append(value)
    lengths: list[int] = []
    for _ in range(num_entries):
        value, pos = _read_varint(directory, pos)
        lengths.append(value)
    offsets: list[int] = []
    previous_end = 0
    for index in range(num_entries):
        value, pos = _read_varint(directory, pos)
        if value == 0 and index > 0:
            offsets.append(previous_end)
        else:
            offsets.append(value - 1)
        previous_end = offsets[-1] + lengths[index]

    tiles: dict[int, bytes] = {}
    for tile_id_value, offset, length in zip(tile_ids, offsets, lengths, strict=True):
        start = header["tileDataOffset"] + offset
        tiles[tile_id_value] = payload[start : start + length]
        assert start + length <= header["tileDataOffset"] + header["tileDataLength"], "瓦片字节越出 tile_data 段"

    metadata = json.loads(
        payload[header["jsonMetadataOffset"] : header["jsonMetadataOffset"] + header["jsonMetadataLength"]].decode("utf-8")
    )
    return {"header": header, "entries": num_entries, "tiles": tiles, "metadata": metadata}


PMTILES_FIRST_CHUNK = 16384
PMTILES_PATH = PUBLIC_DIR / "basemaps" / "aegis.pmtiles"


def test_pmtiles_header_declares_raster_and_zooms() -> None:
    decoded = decode_pmtiles(PMTILES_PATH.read_bytes())
    header = decoded["header"]
    assert header["tileType"] == 2, "tileType 必须是 PNG，否则前端判为不可作影像"
    assert header["minZoom"] == 0 and header["maxZoom"] == 2
    assert header["internalCompression"] == 1 and header["tileCompression"] == 1
    assert header["clustered"] is True
    assert header["numTileEntries"] == header["numTileContents"] == decoded["entries"] == 21
    assert header["numAddressedTiles"] >= header["numTileEntries"]
    assert -180.0 <= header["minLon"] < header["maxLon"] <= 180.0
    assert "合成" in decoded["metadata"]["attribution"]


def test_pmtiles_header_bounds_stay_inside_web_mercator() -> None:
    """头部边界会被前端直接当成影像 provider 的矩形：越出 Web Mercator 纬度上界，
    Cesium 的 positionToTileXY 返回 undefined，`undefined.x` 当场打死渲染循环（浏览器实测）。
    1e7 定点量化必须往框内缩，且不能缩出可见的空隙（只允许差一个量化单位）。
    """
    header = decode_pmtiles(PMTILES_PATH.read_bytes())["header"]
    limit = BUILDER.WEB_MERCATOR_MAX_LATITUDE_DEG
    assert -limit <= header["minLat"] < header["maxLat"] <= limit
    assert limit - 1e-7 < header["maxLat"]
    assert header["minLat"] < -limit + 1e-7
    # 反面教材写成断言：当初就是 round 把 85.05112877980659 写成 850511288（=85.0511288）才越界的。
    assert round(limit * BUILDER.PMTILES_COORD_FIXED_POINT) / BUILDER.PMTILES_COORD_FIXED_POINT > limit


def test_web_mercator_latitude_limit_matches_the_frontend_stub() -> None:
    """这个数在烘焙脚本和前端测试替身里各写了一份；两份不一致就等于自己跟自己核对。
    读源对源，而不是把它抄进第三个地方。"""
    stub = REPO_ROOT / "frontend" / "src" / "testing" / "cesiumBasemapStub.ts"
    assert stub.is_file(), f"跨端门禁要读的前端替身不在位：{stub}"
    assert f"{BUILDER.WEB_MERCATOR_MAX_LATITUDE_DEG!r}" in stub.read_text(encoding="utf-8")


def test_pmtiles_tiles_are_decodable_png_of_right_size() -> None:
    decoded = decode_pmtiles(PMTILES_PATH.read_bytes())
    for tile_id_value, payload in decoded["tiles"].items():
        assert payload.startswith(b"\x89PNG\r\n\x1a\n"), f"tile_id={tile_id_value} 不是 PNG"
        width, height = struct.unpack(">2I", payload[16:24])
        assert (width, height) == (256, 256)
        assert len(payload) > 100, "瓦片小到不可能是真图"
    assert struct.unpack(">2I", decoded["tiles"][BUILDER.tile_id(0, 0, 0)][16:24]) == (256, 256)


def test_pmtiles_root_directory_offsets_point_at_distinct_tiles() -> None:
    decoded = decode_pmtiles(PMTILES_PATH.read_bytes())
    payloads = list(decoded["tiles"].values())
    assert len({hashlib.sha256(p).hexdigest() for p in payloads}) > 1, "所有瓦片字节相同 = 渲染根本没起作用"


def test_bake_runs_from_any_working_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """默认落盘目录锚在脚本位置而不是 cwd：在 backend/ 下随手跑一次，就会在 backend/frontend/public
    里长出一套没人看的瓦——这一条把那次事故钉住。"""
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "public"
    assert BUILDER.main(["--public", str(target), "--max-zoom", "0", "--no-pmtiles"]) == 0
    assert (target / "terrain" / "layer.json").is_file()
    assert len(list((target / "terrain").glob("*/*/*.terrain"))) == 2
    assert BUILDER.DEFAULT_PUBLIC_DIR == PUBLIC_DIR


def test_baking_is_deterministic(tmp_path: Path) -> None:
    """同一脚本二次烘焙必须逐字节一致，否则"资产与生成器对得上"这条断言无法长期成立。"""
    first = {path.relative_to(PUBLIC_DIR): path.read_bytes() for path in baked_terrain_tiles()}
    first_layer = (PUBLIC_DIR / "terrain" / "layer.json").read_bytes()
    out_dir = tmp_path / "public"
    BUILDER.bake(out_dir / "basemaps", out_dir / "terrain", z_max=2, pmtiles=True, xyz=False)
    second = {path.relative_to(out_dir): path.read_bytes() for path in sorted((out_dir / "terrain").glob("*/*/*.terrain"))}
    assert set(second) == set(first)
    for key in second:
        assert hashlib.sha256(first[key]).hexdigest() == hashlib.sha256(second[key]).hexdigest(), f"{key} 二次烘焙不一致"
    assert (out_dir / "terrain" / "layer.json").read_bytes() == first_layer
    assert (out_dir / "basemaps" / "aegis.pmtiles").read_bytes() == PMTILES_PATH.read_bytes()


# ---------- 上游读取器漂移守卫 ----------
#
# 上面的解码器是"按上游源码逐字段镜像"写出来的。它自己不会错，但上游会改：
# 改了之后这些断言就该红，而不是让浏览器里白屏。node_modules 不在版本控制里时跳过（CI 装了就会跑）。


@pytest.mark.skipif(not CESIUM_SOURCE.is_file(), reason="未安装 @cesium/engine 源码（npm install 后才会跑）")
def test_cesium_quantized_mesh_offsets_have_not_drifted() -> None:
    text = CESIUM_SOURCE.read_text(encoding="utf-8")
    start = text.index("function createQuantizedMeshTerrainData")
    body = text[start : text.index("\nfunction ", start + 10)]
    sequence = [
        "let pos = 0;",
        "view.getFloat64(pos, true)",
        "const minimumHeight = view.getFloat32(pos, true)",
        "const maximumHeight = view.getFloat32(pos, true)",
        "view.getFloat64(pos + cartesian3Length, true)",
        "const vertexCount = view.getUint32(pos, true)",
        "AttributeCompression.zigZagDeltaDecode(uBuffer, vBuffer, heightBuffer)",
        "const triangleCount = view.getUint32(pos, true)",
        "const westVertexCount = view.getUint32(pos, true)",
        "const extensionId = view.getUint8(pos, true)",
        "const extensionLength = view.getUint32(pos, littleEndianExtensionSize)",
    ]
    cursor = 0
    for marker in sequence:
        found = body.find(marker, cursor)
        assert found >= 0, f"Cesium 解析顺序变了：找不到 {marker!r}（本脚本的镜像解码器需要跟着改）"
        cursor = found + len(marker)
    assert "0x024b" not in text.lower(), "这版 Cesium 不再从 pos=0 读 center 了：需要先确认前导魔数口径"


@pytest.mark.skipif(not PMTILES_SOURCE.is_file(), reason="未安装 pmtiles 源码（npm install 后才会跑）")
def test_pmtiles_header_offsets_and_enums_have_not_drifted() -> None:
    text = PMTILES_SOURCE.read_text(encoding="utf-8")
    for offset in (8, 16, 24, 32, 56, 64, 72, 80, 88, 96, 97, 98, 99, 100, 101, 102, 118, 119, 123):
        assert f"{offset}" in text, f"pmtiles 头解析里找不到偏移 {offset}"
    assert "getUint8(99)" in text, "tileType 偏移变了"
    assert "getUint8(101)" in text, "maxZoom 偏移变了"
    assert "const HEADER_SIZE_BYTES = 127;" in text
    assert "await source.getBytes(0, 16384)" in text, "首个块大小变了：根目录必须仍然落在里面"
    assert "Png = 2" in text and "Gzip = 2" in text and "None = 1" in text
