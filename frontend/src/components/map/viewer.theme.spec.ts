/**
 * `viewer.ts` 的主题装配（阶段 1 后半 · Cesium 那一面的另一半证据）。
 *
 * 为什么在单测里造一个假 Cesium：地图页的图元色是在 `entities.add()` 那一刻烤进去的，
 * 所以"翻主题"必须把图层重画。这件事真机上不好钉——本机的 `public/basemaps/` 只有 README 与
 * pmtiles，区域锚点与灾害面两份烘焙资产不在，站点与预警全进「未定位清单」，场景里一颗图元都没有
 * （第一版判据就是这么红的：`points: []`）。为了这条去伪造一份锚点坐标，等于踩"不硬编造事实"那条红线。
 * 所以分工改成：真机钉**画面级**（地球底/画面外底/天空大气，见 e2e/map-visual.spec.ts），
 * 这里用假 Cesium 钉**装配逻辑**（建场即按当前档上色；翻档时换调色板并重画图元）。
 *
 * 假 Cesium 只做本文件用到的那些形状，故意不实现任何渲染——被测的是"我们把什么颜色交给 Cesium"。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

import { colorSlots, riskFill } from '@/theme/tokens'

interface FakeColor {
  red: number
  green: number
  blue: number
  alpha: number
  toCssColorString(): string
  withAlpha(alpha: number): FakeColor
}

function parseCss(css: string): [number, number, number] {
  const hex = css.replace('#', '')
  const full = hex.length === 3 ? hex.split('').map((ch) => ch + ch).join('') : hex.slice(0, 6)
  return [
    Number.parseInt(full.slice(0, 2), 16),
    Number.parseInt(full.slice(2, 4), 16),
    Number.parseInt(full.slice(4, 6), 16),
  ]
}

function color(css: string): FakeColor {
  const [red, green, blue] = parseCss(css)
  const make = (alpha: number): FakeColor => ({
    red,
    green,
    blue,
    alpha,
    toCssColorString: () =>
      alpha >= 1
        ? `#${[red, green, blue].map((v) => v.toString(16).padStart(2, '0')).join('')}`
        : `rgba(${red},${green},${blue},${alpha})`,
    withAlpha: (next: number) => make(next),
  })
  return make(1)
}

interface CapturedEntity {
  id: string
  point?: { color?: FakeColor; outlineColor?: FakeColor }
  label?: { fillColor?: FakeColor; outlineColor?: FakeColor; backgroundColor?: FakeColor }
}

class EntityCollection {
  readonly items: CapturedEntity[] = []
  addCount = 0
  removeAllCount = 0
  add(entity: CapturedEntity): CapturedEntity {
    this.items.push(entity)
    this.addCount += 1
    return entity
  }
  removeAll(): void {
    this.items.length = 0
    this.removeAllCount += 1
  }
}

class FakeDataSource {
  readonly entities = new EntityCollection()
  show = true
  constructor(readonly name: string) {}
}

function buildFakeCesium(captured: { dataSources: FakeDataSource[] }) {
  const scene = {
    globe: { baseColor: color('#000000'), enableLighting: false, imageryLayers: [] as unknown[] },
    backgroundColor: color('#000000'),
    skyAtmosphere: { show: true },
    sun: { show: true },
    moon: { show: true },
    screenSpaceCameraController: {} as Record<string, number>,
    canvas: {} as unknown,
    requestRenderCount: 0,
    requestRender() {
      scene.requestRenderCount += 1
    },
    pick: () => undefined,
  }
  const camera = {
    percentageChanged: 0,
    positionCartographic: { height: 1_000_000 },
    setView: () => {},
    flyTo: () => {},
    changed: { addEventListener: () => {}, removeEventListener: () => {} },
  }
  const viewer = {
    scene,
    camera,
    dataSources: {
      add(source: FakeDataSource) {
        captured.dataSources.push(source)
      },
      removeAll: () => {},
    },
    destroy: () => {},
  }
  return {
    Viewer: function ViewerStub() {
      return viewer
    },
    CustomDataSource: function DataSourceStub(name: string) {
      return new FakeDataSource(name)
    },
    Color: { fromCssColorString: (css: string) => color(css) },
    Cartesian3: { fromDegrees: (lon: number, lat: number, height = 0) => ({ lon, lat, height }) },
    Cartesian2: function Cartesian2Stub(x: number, y: number) {
      return { x, y }
    },
    Rectangle: { fromDegrees: (west: number, south: number, east: number, north: number) => ({ west, south, east, north }) },
    PolygonHierarchy: function PolygonHierarchyStub(outer: unknown, holes?: unknown) {
      return { outer, holes }
    },
    Math: { toDegrees: (radians: number) => (radians * 180) / Math.PI },
    ClassificationType: { TERRAIN: 'TERRAIN' },
    HeightReference: { CLAMP_TO_GROUND: 'CLAMP' },
    ScreenSpaceEventHandler: function HandlerStub() {
      return { setInputAction: () => {}, removeInputAction: () => {}, destroy: () => {} }
    },
    ScreenSpaceEventType: { LEFT_CLICK: 'LEFT_CLICK' },
    Credit: function CreditStub(text: string) {
      return { text }
    },
    CreditDisplay: { cesiumCredit: null as unknown },
    __scene: scene,
    __viewer: viewer,
  }
}

const captured = { dataSources: [] as FakeDataSource[] }
const fakeCesium = buildFakeCesium(captured)

vi.mock('cesium', () => fakeCesium)
vi.mock('@/components/map/offline', async () => {
  const actual = await vi.importActual<typeof import('@/components/map/offline')>('@/components/map/offline')
  return {
    ...actual,
    collectFacts: async () => ({
      browserOnline: true,
      apiReachable: true,
      basemapReachable: false,
      terrainReachable: false,
      localBasemapReachable: false,
      checkedAt: 0,
    }),
  }
})
vi.mock('@/components/map/terrain', async () => {
  const actual = await vi.importActual<typeof import('@/components/map/terrain')>('@/components/map/terrain')
  return { ...actual, createTerrainSetup: async () => ({ provider: {}, message: '椭球面兜底', mode: 'ellipsoid' as const }) }
})
vi.mock('@/components/map/basemap', async () => {
  const actual = await vi.importActual<typeof import('@/components/map/basemap')>('@/components/map/basemap')
  return { ...actual, createBasemapSetup: async () => ({ layer: {}, message: '无影像', kind: 'none' as const }) }
})

/** 五个图层 + 一颗高危站点 + 一条劣化触达：够覆盖"图元色从调色板来"这件事。 */
function planWithFeatures(): any {
  const station = {
    id: 'st-1',
    title: '某站',
    nameZh: '某站',
    lonlat: { lon: 91.0, lat: 29.0 },
    elevationM: null,
    riskLevel: 1,
    colorCss: riskFill.light[1],
  }
  const warning = { id: 'wrn-1', title: '红色预警', lonlat: { lon: 92.0, lat: 30.0 }, colorCss: riskFill.light[1] }
  const reach = { id: 'reach-1', title: '短信通道', lonlat: { lon: 93.0, lat: 31.0 }, colorCss: '#cf1322' }
  const cluster = { id: 'cl-1', lonlat: { lon: 94.0, lat: 32.0 }, count: 4, colorCss: riskFill.light[2] }
  const zone = { id: 'hz-1', title: '滑坡区', outer: [{ lon: 90, lat: 28 }], holes: [], colorCss: riskFill.light[3] }
  return { hazards: [zone], stations: [station], warnings: [warning], reach: [reach], clusters: [cluster], clustered: false }
}

async function buildScene(themeMode: 'light' | 'dark') {
  const { createMapScene } = await import('@/components/map/viewer')
  return createMapScene({} as HTMLElement, { themeMode, origin: 'http://127.0.0.1:4175' })
}

function layerOf(name: string): EntityCollection {
  const found = captured.dataSources.find((source) => source.name === `aegis-${name}`)
  if (!found) throw new Error(`没有 ${name} 图层：装配面改了？`)
  return found.entities
}

beforeEach(() => {
  captured.dataSources.length = 0
})

describe('viewer.ts 的主题装配', () => {
  it('建场就把地球底与画面外底换成当前档的令牌值，并关掉太阳/月亮/天空大气', async () => {
    const scene = await buildScene('dark')
    const globe = (fakeCesium as any).__scene.globe
    expect(globe.baseColor.toCssColorString().toLowerCase(), '地球底不是深色档的 sunken').toBe(colorSlots.dark.sunken.toLowerCase())
    expect((fakeCesium as any).__scene.backgroundColor.toCssColorString().toLowerCase()).toBe(colorSlots.dark.page.toLowerCase())
    expect((fakeCesium as any).__scene.skyAtmosphere.show, '天空大气还开着，等于留一条不肯翻面的亮面').toBe(false)
    expect((fakeCesium as any).__scene.sun.show).toBe(false)
    expect((fakeCesium as any).__scene.moon.show).toBe(false)
    expect(scene.palette.mode).toBe('dark')
    scene.destroy()
  })

  it('图元颜色全部来自调色板：标记描边走 text 槽，风险填充走当前档', async () => {
    const scene = await buildScene('light')
    scene.render(planWithFeatures())
    const station = layerOf('stations').items[0]
    expect(station.point?.outlineColor?.toCssColorString().toLowerCase(), '站点描边不是浅色档 text').toBe(colorSlots.light.text.toLowerCase())
    expect(station.point?.color?.toCssColorString().toLowerCase()).toBe(riskFill.light[1].toLowerCase())

    const reach = layerOf('reach').items[0]
    expect(reach.label?.fillColor?.toCssColorString().toLowerCase(), '触达标签字色不是 text 槽').toBe(colorSlots.light.text.toLowerCase())
    scene.destroy()
  })

  it('翻主题时既换画面底色，也把图层重画（图元色是 add 时烤进去的，不重画就是半翻画面）', async () => {
    const scene = await buildScene('light')
    scene.render(planWithFeatures())
    const stations = layerOf('stations')
    const addsBefore = stations.addCount
    const deepBefore = stations.items[0]!.point!.outlineColor!.toCssColorString()

    scene.setTheme('dark')

    expect(scene.palette.mode, '调色板没跟着翻档').toBe('dark')
    expect((fakeCesium as any).__scene.globe.baseColor.toCssColorString().toLowerCase()).toBe(colorSlots.dark.sunken.toLowerCase())
    expect(stations.addCount, '图层没有重建：只换了底色，图元还是上一档的颜色').toBe(addsBefore + 1)
    expect(stations.removeAllCount, '重建前没有清空旧图元').toBeGreaterThan(0)
    const deepAfter = stations.items[0]!.point!.outlineColor!.toCssColorString()
    expect(deepAfter, '翻转后描边还是浅色档那一支').not.toBe(deepBefore)
    expect(deepAfter.toLowerCase()).toBe(colorSlots.dark.text.toLowerCase())
    scene.destroy()
  })

  it('深色档把浅档风险色逐档换过去，认不出的（等级未知灰）原样画出来', async () => {
    const scene = await buildScene('dark')
    const plan = planWithFeatures()
    plan.reach[0].colorCss = '#bfbfbf'
    scene.render(plan)
    expect(layerOf('stations').items[0]!.point!.color!.toCssColorString().toUpperCase()).toBe(riskFill.dark[1].toUpperCase())
    expect(layerOf('reach').items[0]!.point!.color!.toCssColorString(), '等级未知灰不该被当成风险档改色').toBe('#bfbfbf')
    scene.destroy()
  })

  it('同一份数据反复 render 不重建（相机每挪一点就重画会掉帧），但翻档必须重建', async () => {
    const scene = await buildScene('light')
    const plan = planWithFeatures()
    scene.render(plan)
    scene.render(plan)
    const stations = layerOf('stations')
    expect(stations.addCount, '要素集合没变却重建了').toBe(1)
    scene.setTheme('dark')
    scene.setTheme('dark')
    expect(stations.addCount, '重复翻到同一档不该再重画').toBe(2)
    scene.destroy()
  })
})
