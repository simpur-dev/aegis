"""把**真实 OSM 数据**烘成同源 PNG 瓦片金字塔（`frontend/public/basemaps/{z}/{x}/{y}.png`）。

为什么另起一个脚本而不是改 `build_offline_tiles.py`：后者产的是**合成**瓦（解析函数画的设色 +
经纬网），用途是"在没有合规底图之前，先证明离线底图链路真能被读通"。本脚本产的是**真数据**：
Geofabrik 的 `.osm.pbf` 经 GDAL 的 OSM 驱动读进来，按类渲染成 Web Mercator 瓦片。
两者共用同一条前端降级链（`basemap.ts` 的 `url-template` 那一档），互不覆盖。

为什么走 XYZ 目录（形态 B）而不是 PMTiles（形态 A）：现有 PMTiles 写入器是**单层根目录**，
且硬校验"根目录 + 元数据必须落在首个 16 KB 内"（见 `build_offline_tiles.py` 的 PMTILES_FIRST_CHUNK_BYTES
注释）。区域档案动辄几千张瓦，目录远超 16 KB，要装得下就得补 leaf 目录——那是另一件事。
形态 B 前端早就支持，零新格式代码。

许可与署名：OSM 数据是 ODbL，**必须署名 © OpenStreetMap contributors**。产出的瓦片树默认不入库
（`frontend/public/basemaps/.gitignore`），交付形态是"跑一次烘焙"或"随镜像带上"。

用法（依赖装在仓库外的独立 venv，步骤见 `frontend/public/basemaps/README.md`）：

    python scripts/bake_osm_basemap.py --pbf E:/tmp/osm/tibet-latest.osm.pbf --max-zoom 10
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import shutil
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

WEB_MERCATOR_MAX_LAT = math.degrees(math.atan(math.sinh(math.pi)))
HALF_CIRCUMFERENCE = 20037508.342789244
TILE_PX = 256

# 只取"一眼看得出众要素"的几类。刻意不碰 multipolygons（ landuse/natural 大面）：
# 那是几十分钟级的裁剪与填充开销，v1 要的是"路网 + 水系 + 界线 + 注记"能不能落对位置。
ROAD_MAJOR = ('motorway', 'trunk', 'primary')
ROAD_MINOR = ('secondary', 'tertiary')
RIVERS = ('river', 'canal')
PLACES = ('city', 'town')

STYLE: dict[str, dict[str, object]] = {
    'river': {'color': '#6fa8dc', 'width': 0.7},
    'rail': {'color': '#9c8f7a', 'width': 0.5},
    'road_minor': {'color': '#b9bfc9', 'width': 0.45},
    'road_major': {'color': '#d9a04a', 'width': 0.9},
}


@dataclass(frozen=True)
class TileJob:
    z: int
    x: int
    y: int


def tile_bounds_meters(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """瓦片的 EPSG:3857 范围（minx, miny, maxx, maxy）。瓦 y 自上而下，mercator y 自下而上。"""
    size = HALF_CIRCUMFERENCE * 2 / (1 << z)
    minx = -HALF_CIRCUMFERENCE + x * size
    maxy = HALF_CIRCUMFERENCE - y * size
    return minx, maxy - size, minx + size, maxy


def tile_range(z: int, bbox: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
    """bbox（经纬度 west,south,east,north）在该层的 x/y 区间（含端点，已夹到合法瓦号）。"""
    west, south, east, north = bbox
    n = 1 << z

    def xf(lon: float) -> float:
        return (lon + 180.0) / 360.0 * n

    def yf(lat: float) -> float:
        clamped = max(-WEB_MERCATOR_MAX_LAT + 1e-9, min(WEB_MERCATOR_MAX_LAT - 1e-9, lat))
        s = math.sin(math.radians(clamped))
        return (1.0 - math.log((1 + s) / (1 - s)) / (2 * math.pi)) / 2.0 * n

    x0, x1 = int(math.floor(xf(west))), int(math.floor(xf(east)))
    y0, y1 = int(math.floor(yf(north))), int(math.floor(yf(south)))
    return max(0, x0), min(n - 1, x1), max(0, y0), min(n - 1, y1)


def _read_layer(pbf: Path, layer: str, where: str, columns: list[str]):
    """读一层并按 SQL 条件过滤。

    **只有"这一层在本数据集里不存在"才算可跳过**；其它异常一律上抛。
    这里原先是个宽 `except Exception`：缺 geopandas 时它把 ImportError 咽成"该区域无此类要素"，
    于是零要素也照样烘出 14 张全透明瓦并打印"完成"——空跑却报成功，比崩掉更糟。
    """
    import pyogrio
    from pyogrio.errors import DataLayerError

    try:
        frame = pyogrio.read_dataframe(pbf, layer=layer, where=where, columns=columns, read_geometry=True)
    except DataLayerError as error:
        print(f'  本数据集没有 {layer} 层，跳过：{str(error)[:100]}', file=sys.stderr)
        return None
    return frame if len(frame) else None


def _in_mercator(frame, names: list[str]) -> list:
    """几何转 EPSG:3857，摊平成 (line, name) 列表——Multi* 拆成单条，否则 matplotlib 吃不下。"""
    from shapely import get_parts

    projected = frame.geometry.to_crs(epsg=3857) if frame.geometry.crs and frame.geometry.crs.to_epsg() != 3857 else frame.geometry
    out: list[tuple[object, str]] = []
    for index, geom in enumerate(projected):
        if geom is None or geom.is_empty:
            continue
        name = names[index] if index < len(names) else ''
        for part in get_parts(geom):
            if part.geom_type.endswith('LineString'):
                out.append((part, name))
            elif part.geom_type.endswith('Point'):
                out.append((part, name))
    return out


def _load_buckets(pbf: Path) -> dict[str, list]:
    quoted = lambda values: ','.join(f"'{v}'" for v in values)  # noqa: E731
    buckets: dict[str, list] = {}

    roads = _read_layer(pbf, 'lines', f"highway IN ({quoted(ROAD_MAJOR)})", ['name', 'highway'])
    if roads is not None:
        buckets['road_major'] = _in_mercator(roads[roads['highway'].isin(ROAD_MAJOR)], list(roads['name'].fillna('')))
    minor = _read_layer(pbf, 'lines', f"highway IN ({quoted(ROAD_MINOR)})", ['name', 'highway'])
    if minor is not None:
        buckets['road_minor'] = _in_mercator(minor, list(minor['name'].fillna('')))
    rivers = _read_layer(pbf, 'lines', f"waterway IN ({quoted(RIVERS)})", ['name', 'waterway'])
    if rivers is not None:
        buckets['river'] = _in_mercator(rivers, list(rivers['name'].fillna('')))
    rail = _read_layer(pbf, 'lines', 'railway IS NOT NULL', ['railway'])
    if rail is not None:
        buckets['rail'] = _in_mercator(rail, [])
    # 行政界线**不在 v1**：GDAL 的默认 OSM 配置把 boundary 关系放进 `multipolygons`
    # （`multilinestrings` 只有 osm_id/name/type/other_tags 四列，实测按 other_tags 模糊匹配是 0 条），
    # 而本脚本刻意不读大面层。要界线得配一份自定义 OSM tags 文件把 admin_level 提到 lines 上——留作后续。
    places = _read_layer(pbf, 'points', "place IN ('city')", ['name', 'place'])
    if places is not None:
        buckets['places_city'] = _in_mercator(places, list(places['name'].fillna('')))
    towns = _read_layer(pbf, 'points', "place IN ('town')", ['name', 'place'])
    if towns is not None:
        buckets['places_town'] = _in_mercator(towns, list(towns['name'].fillna('')))
    return buckets


# 中文字形：matplotlib 默认的 DejaVu Sans 没有 CJK，注会画成豆腐块。
# 顺序按**许可**排（OFL 的可随镜像分发，微软那几支只是本机临时用），且刻意不收 .ttc——
# 实测 `msyh.ttc` 能被 font_manager 登记、`findfont` 也解析得回去，但渲出来的中文仍是方块：
# "文件在、注册成功"都不证明字形取得到，所以下面用"实渲一个汉字数墨"来判，而不是看文件存在。
CJK_FONT_CANDIDATES = [
    ('Noto Sans SC', [r'C:\Windows\Fonts\Noto Sans SC (TrueType).otf', r'C:\Windows\Fonts\NotoSansSC-VF.ttf']),
    ('Source Han Sans SC', [r'C:\Windows\Fonts\SourceHanSansSC-Regular.otf']),
    ('SimHei', [r'C:\Windows\Fonts\simhei.ttf']),
]

# 判"能不能画中文"的样字：这些码位都在字体 cmap 里，才算这支可用
CJK_PROBE_TEXT = '西藏'


def _font_inks_cjk(name: str, path: str) -> bool:
    """判这套字体到底取不取得到中文字形：**直接读 cmap**。

    为什么不用"渲出来看像素"：不同字体的 `.notdef` 方框长得不一样（Arial 的和 DejaVu 的就不同），
    所以"与无中文字体的渲染结果比对"会把 Arial 也判成可用——实测两轮反例都把它驳回了。
    `FT2Font` 随 matplotlib 一起装，读码位→字形的映射是唯一不含糊的判法。
    """
    from matplotlib import font_manager
    from matplotlib.ft2font import FT2Font

    try:
        face = FT2Font(path)
    except Exception as error:  # noqa: BLE001 - 打不开的文件（含 .ttc 集合）就是不可用
        print(f'  字体读不动 {path}：{type(error).__name__}: {str(error)[:80]}', file=sys.stderr)
        return False
    charmap = face.get_charmap()
    if not all(ord(ch) in charmap for ch in CJK_PROBE_TEXT):
        return False
    try:
        font_manager.fontManager.addfont(path)
    except Exception as error:  # noqa: BLE001
        print(f'  字体注册失败 {path}：{type(error).__name__}: {error}', file=sys.stderr)
        return False
    return True


def pick_cjk_font() -> tuple[str | None, str | None]:
    """返回第一个**实测能画中文**的 (字体名, 路径)；全都不行就 (None, None)。"""
    for name, paths in CJK_FONT_CANDIDATES:
        for path in paths:
            if Path(path).is_file() and _font_inks_cjk(name, path):
                return name, path
    return None, None


_WORKER: dict[str, object] = {}


def _init_worker(pbf: str) -> None:
    """每个进程读一次 pbf 并**建好 STRtree**：逐要素×逐瓦的相交判定在这个量级上跑不完。"""
    from shapely import STRtree

    # 中文字形要在建图之前定：matplotlib 默认的 DejaVu Sans 没有 CJK，注记会画成豆腐块。
    font_name, font_path = pick_cjk_font()
    if font_path:
        import matplotlib
        from matplotlib import font_manager

        font_manager.fontManager.addfont(font_path)
        matplotlib.rcParams['font.family'] = font_name
    _WORKER['font'] = font_name

    buckets = _load_buckets(Path(pbf))
    if not any(buckets.values()):
        # 一个桶都没命中就别往下画了：全透明瓦片看起来"烘成功了"，其实什么都没验到。
        raise RuntimeError(f'{pbf} 里没读到任何目标要素（检查 pbf 是否完整、GDAL OSM 驱动是否可用）')
    index: dict[str, tuple[STRtree | None, list]] = {}
    for name, items in buckets.items():
        geoms = [item[0] for item in items]
        index[name] = (STRtree(geoms) if geoms else None, items)
    _WORKER['index'] = index
    counts = {name: len(items) for name, (_tree, items) in index.items()}
    print(f'  worker 就绪（注记字体 {font_name or "无 CJK 字体：中文将缺字"}）：{counts}', file=sys.stderr)


def render_tile(job: TileJob) -> tuple[TileJob, bytes | None]:
    """画一张瓦：透明底 + 界线/水/路/注记。透明是刻意的——地球底色与地形由 Cesium 那侧给。"""
    import matplotlib

    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection
    from matplotlib.patheffects import withStroke
    from shapely.geometry import box

    index: dict = _WORKER.get('index') or {}
    minx, miny, maxx, maxy = tile_bounds_meters(job.z, job.x, job.y)
    clip = box(minx, miny, maxx, maxy)

    fig = plt.figure(figsize=(TILE_PX / 100, TILE_PX / 100), dpi=100)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(minx, maxx)
    ax.set_ylim(miny, maxy)
    ax.set_axis_off()
    ax.patch.set_alpha(0.0)
    fig.patch.set_alpha(0.0)

    for bucket in ('river', 'rail', 'road_minor', 'road_major'):
        tree, items = index.get(bucket, (None, []))
        if tree is None:
            continue
        style = STYLE[bucket]
        segments: list[list[tuple[float, float]]] = []
        for hit in sorted(tree.query(clip)):
            line, _name = items[hit]
            clipped = line.intersection(clip)
            if clipped.is_empty:
                continue
            # 跨界瓦的 intersection 可能仍是 MultiLineString
            from shapely import get_parts

            for part in get_parts(clipped):
                if hasattr(part, 'coords'):
                    segments.append(list(part.coords))
        if segments:
            dash = style.get('dash')
            ax.add_collection(
                LineCollection(
                    segments,
                    colors=style['color'],
                    linewidths=style['width'],
                    linestyles=(0, dash) if dash else 'solid',
                    capstyle='round',
                )
            )

    places_city, city = index.get('places_city', (None, []))
    places_town, town = index.get('places_town', (None, []))
    # 字号：dpi=100 下 1pt≈1.39px，而汉字要 ≥12px 才可读——原先给 5pt（≈7px）
    # 直接把注记糊成一片方块，看着像"字体缺字"，其实是太小（40pt 实测笔画完全正常）。
    label_size = 9.5 if job.z <= 8 else 8.5
    wanted = [('city', places_city, city)]
    if job.z >= 8:
        wanted.append(('town', places_town, town))
    for _label, tree, items in wanted:
        if tree is None:
            continue
        for hit in sorted(tree.query(clip)):
            point, name = items[hit]
            if name:
                ax.text(
                    point.x,
                    point.y,
                    name,
                    fontsize=label_size,
                    color='#1c2028',
                    ha='left',
                    va='center',
                    path_effects=[withStroke(linewidth=2.0, foreground='white')],
                )

    buf = io.BytesIO()
    fig.savefig(buf, format='png', transparent=True, pad_inches=0)
    plt.close(fig)
    return job, buf.getvalue()


def _spread_sample(paths: list[Path], size: int = 60) -> list[Path]:
    """等距抽样，而不是"前 N 个"。

    实测踩过：写入顺序是 z 升序、x 升序、y 升序，取前 40 个等于只看该层**西北边缘**那一排，
    而西藏框的西北角大片是无人区空格——于是一次成功的烘焙被自检判成"全透明、渲染链断了"。
    判据抓不到东西时，先怀疑采样，再下结论。
    """
    if len(paths) <= size:
        return paths
    step = len(paths) / size
    return [paths[int(i * step)] for i in range(size)]


def _count_inked(paths: list[Path]) -> int:
    """数有几张瓦真的画上了非透明像素（阈值：alpha>0 的像素数 ≥ 8）。"""
    from PIL import Image

    inked = 0
    for path in paths:
        try:
            with Image.open(path) as image:
                if image.mode != 'RGBA':
                    inked += 1
                    continue
                # histogram 而不是 getdata：后者在 Pillow 13 起弃用，且逐像素遍历 65k 个点没必要
                if sum(image.getchannel('A').histogram()[1:]) >= 8:
                    inked += 1
        except Exception:  # noqa: BLE001 - 打不开就是没画上，计入"零"而不是崩在这里
            continue
    return inked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='把真实 OSM pbf 烘成同源 PNG 瓦片金字塔（形态 B）')
    parser.add_argument('--pbf', required=True, help='Geofabrik 的 *.osm.pbf 路径')
    parser.add_argument('--out', default='frontend/public/basemaps', help='输出目录（默认前端同源资产目录）')
    parser.add_argument('--min-zoom', type=int, default=5)
    parser.add_argument('--max-zoom', type=int, default=10)
    parser.add_argument('--bbox', default='', help='west,south,east,north；缺省用 pbf 自带范围')
    parser.add_argument('--jobs', type=int, default=0, help='并行进程数，0=自动（封顶 8）')
    parser.add_argument('--clean', action='store_true', help='先删输出目录里的数字 z 层目录')
    parser.add_argument('--verify-only', action='store_true', help='不烘，只审计输出目录里已有的瓦（交付后复核用）')
    args = parser.parse_args(argv)

    out = Path(args.out)
    if args.verify_only:
        existing = sorted(out.glob('[0-9]*/*/*.png'))
        if not existing:
            print(f'{out} 下一张瓦都没有——别把"没烘"审计成"烘坏了"', file=sys.stderr)
            return 1
        sample = _spread_sample(existing)
        inked = _count_inked(sample)
        levels = sorted({int(p.parent.parent.name) for p in existing})
        print(f'已有 {len(existing):,} 张（z{levels[0]}–z{levels[-1]}）；等距抽检 {len(sample)} 张，有内容 {inked} 张')
        if inked == 0:
            print('抽检全透明：渲染链或数据有问题', file=sys.stderr)
            return 1
        return 0

    pbf = Path(args.pbf)
    if not pbf.is_file():
        print(f'pbf 不在位：{pbf}', file=sys.stderr)
        return 2

    import pyogrio

    # 多图层 pbf 上不带 layer 的 read_info 没有 feature_count（且会告警"图层不唯一"），
    # 范围取 lines 层就够——它就是我们要画的那些要素的范围。
    info = pyogrio.read_info(pbf, layer='lines')
    if args.bbox:
        bbox = tuple(float(v) for v in args.bbox.split(','))
    else:
        bounds = info['total_bounds']
        bbox = (float(bounds[0]), float(bounds[1]), float(bounds[2]), float(bounds[3]))
    print(f'源 {pbf.name}：范围 {tuple(round(v, 2) for v in bbox)}（OSM 驱动不预扫要素数，这里不报）')

    if args.clean and out.is_dir():
        for child in out.iterdir():
            if child.is_dir() and child.name.isdigit():
                shutil.rmtree(child)

    jobs: list[TileJob] = []
    for z in range(args.min_zoom, args.max_zoom + 1):
        x0, x1, y0, y1 = tile_range(z, bbox)
        jobs.extend(TileJob(z, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1))
    if not jobs:
        print('层级与范围没有交集，什么都没烘', file=sys.stderr)
        return 1
    print(f'待烘 {len(jobs):,} 张（z{args.min_zoom}–z{args.max_zoom}）')

    # 先在父进程读一遍：**坏查询/缺依赖要在这里报出人话**，而不是让 worker 死在池子里
    # 只剩一句 BrokenProcessPool（实测就这么丢过一个"where 条件无效"的真错误）。
    parent_buckets = _load_buckets(pbf)
    counts = {name: len(items) for name, items in parent_buckets.items()}
    print(f'命中要素：{counts}')
    if not any(parent_buckets.values()):
        print('一条要素都没命中——pbf 不完整、字段名不匹配或驱动缺位，别往下烘全透明瓦', file=sys.stderr)
        return 1
    del parent_buckets

    workers = args.jobs or min(os.cpu_count() or 4, 8)
    done = 0
    written: list[Path] = []
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker, initargs=(str(pbf),)) as pool:
        for job, png in pool.map(render_tile, jobs, chunksize=32):
            if png is None:
                continue
            target = out / str(job.z) / str(job.x)
            target.mkdir(parents=True, exist_ok=True)
            path = target / f'{job.y}.png'
            path.write_bytes(png)
            written.append(path)
            done += 1
            if done % 250 == 0:
                print(f'  {done}/{len(jobs)} …', flush=True)

    # 自证"真的画上东西了"：等距抽样数非透明像素。全 0 就是渲染链断了，
    # 而产物看起来与成功时一模一样（文件在、尺寸对、能打开）。
    sample = _spread_sample(written)
    inked = _count_inked(sample)
    print(f'抽检 {len(sample)} 张：有内容的 {inked} 张')
    if inked == 0:
        print('烘出来的瓦全透明——渲染链断了，别当成成功', file=sys.stderr)
        return 1

    (out).mkdir(parents=True, exist_ok=True)
    (out / 'bake-meta.json').write_text(
        json.dumps(
            {
                'source': 'OpenStreetMap（Geofabrik extract）',
                'attribution': '© OpenStreetMap contributors (ODbL)',
                'pbf': pbf.name,
                'bbox': list(bbox),
                'min_zoom': args.min_zoom,
                'max_zoom': args.max_zoom,
                'tiles': done,
                # 字形被烤进栅格了，用的是哪套字体有许可含义，必须留痕
                'label_font': pick_cjk_font()[0],
                'generator': 'scripts/bake_osm_basemap.py',
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )
    print(f'完成 {done:,} 张 → {out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
