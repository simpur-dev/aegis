"""Build the offline Gyirong close-ups from Sentinel-2, Vantor and OpenStreetMap.

Run with: uv run --with rasterio --with pillow python scripts/bake-gyirong-closeup.py --osm <extract.osm>
"""

from __future__ import annotations

import argparse
import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
METADATA = ROOT / "src/components/map/gyirong-relief.json"
GEOGRAPHY = ROOT / "src/components/map/gyirong-geography.json"
ROAD_TYPES = {"primary", "secondary", "tertiary", "unclassified", "residential", "service", "living_street", "track"}


def write_image(config: dict, raster: Path | None = None) -> None:
    import numpy as np
    import rasterio
    from PIL import Image
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds
    from rasterio.vrt import WarpedVRT

    bounds = config["bounds"]
    west, south, east, north = (bounds[key] for key in ("west", "south", "east", "north"))
    height = round((north - south) * 110_574 / config["resolutionM"])
    width = round((east - west) * 111_320 * math.cos(math.radians((south + north) / 2)) / config["resolutionM"])
    transform = from_bounds(west, south, east, north, width, height)
    source_path = raster or config.get('asset')
    if source_path is None:
        with urlopen(config["catalog"], timeout=20) as response:
            source_path = json.load(response)["assets"]["visual"]["href"]
    open_options = {'overview_level': config['overviewLevel']} if 'overviewLevel' in config else {}
    with (
        rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif", GDAL_HTTP_TIMEOUT="45"),
        rasterio.open(source_path, **open_options) as source,
        WarpedVRT(source, crs="EPSG:4326", width=width, height=height, transform=transform, resampling=Resampling.bilinear) as view,
    ):
        rgb = view.read([1, 2, 3]).transpose(1, 2, 0)
        mask = view.dataset_mask()
    if (mask == 0).any():
        raise ValueError("The source scene does not cover the entire close-up")
    # The Vantor scene is low contrast. Stretch luminance and keep the original color channels.
    if config['resolutionM'] < 1:
        source_rgb = rgb.astype(np.float32)
        luminance = source_rgb @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
        low, high = np.percentile(luminance, [2, 98])
        mapped = 40 + (luminance - low) * 146 / max(high - low, 1)
        rgb = np.round(np.clip(source_rgb + (mapped - luminance)[..., None], 0, 255)).astype(np.uint8)
    else:
        rgb = np.round(255 * (rgb.astype(float) / 255) ** 0.84).astype(np.uint8)
    yy, xx = np.ogrid[:height, :width]
    distance = np.minimum(np.minimum(xx, width - 1 - xx), np.minimum(yy, height - 1 - yy))
    fade = np.minimum(distance / 26, 1)
    alpha = np.round(fade * fade * (3 - 2 * fade) * 255).astype(np.uint8)
    image = Image.fromarray(np.dstack([rgb, alpha]))
    image.save(ROOT / 'public' / config['url'].lstrip('/'), quality=92, lossless=config['resolutionM'] >= 1)
    print(f"{config['provider']}: {config['capturedAt']} · {width}×{height}")


def write_features(osm: Path, bounds: dict) -> None:
    source = ET.parse(osm).getroot()
    nodes = {node.get('id'): (round(float(node.attrib['lon']), 7), round(float(node.attrib['lat']), 7)) for node in source.findall('node')}
    groups: dict[str, list] = {key: [] for key in ('localRoads', 'buildings', 'riverBanks')}
    for way in source.findall('way'):
        tags = {tag.get('k'): tag.get('v') for tag in way.findall('tag')}
        if tags.get('building') not in (None, 'no'):
            group = 'buildings'
        elif (tags.get('natural') == 'water' and tags.get('water') == 'river') or tags.get('waterway') == 'riverbank':
            group = 'riverBanks'
        elif tags.get('highway') in ROAD_TYPES:
            group = 'localRoads'
        else:
            continue
        refs = [nd.get('ref') for nd in way.findall('nd')]
        if not all(ref in nodes for ref in refs):
            continue
        pts = [nodes[ref] for ref in refs]
        if not any(bounds['west'] <= lon <= bounds['east'] and bounds['south'] <= lat <= bounds['north'] for lon, lat in pts):
            continue
        if group != 'localRoads' and (len(pts) < 4 or pts[0] != pts[-1]):
            continue
        if group == 'localRoads' and len(pts) < 2:
            continue
        groups[group].append({'osmId': int(way.attrib['id']), 'coordinates': pts})

    data = json.loads(GEOGRAPHY.read_text(encoding='utf-8'))
    data['closeup'] = groups
    # Keep the surveyed feature coordinates compact and the existing reference lines readable.
    output = json.dumps(data, ensure_ascii=False, indent=2)
    start = output.index('  "closeup":')
    compact = ',\n'.join(f'    "{key}": [\n' + ',\n'.join('      ' + json.dumps(item, separators=(',', ':')) for item in items) + '\n    ]' for key, items in groups.items())
    GEOGRAPHY.write_text(output[:start] + '  "closeup": {\n' + compact + '\n  }\n}\n', encoding='utf-8')
    print('OSM:', {key: len(items) for key, items in groups.items()})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--osm', type=Path)
    parser.add_argument('--product', choices=('closeup', 'street'))
    parser.add_argument('--raster', type=Path, help='Local copy of the GeoTIFF for the selected product')
    args = parser.parse_args()
    if args.raster and not args.product:
        parser.error('--raster requires --product')
    config = json.loads(METADATA.read_text(encoding='utf-8'))
    for key in (args.product,) if args.product else ('closeup', 'street'):
        write_image(config[key], args.raster)
    if args.osm:
        write_features(args.osm, config['closeup']['bounds'])


if __name__ == '__main__':
    main()
