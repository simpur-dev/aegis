<script setup lang="ts">
/**
 * MapPanel.vue —— 「一张图」右侧信息面板（纯展示组件）。
 *
 * 只有 props/emits：不取数、不判断、不认识 Cesium。
 * 所有字段都是 entities.ts / offline.ts 产出的 plain data，
 * 因此本组件的可测性靠那两层保证，这里不再重复逻辑（避免同一规则两处实现）。
 */

import { computed } from 'vue'

import type {
  FeatureDetail,
  LayerId,
  LayerVisibility,
  PlanLayer,
  RejectedGeometry,
  UnlocatedItem,
  UnlocatedReason,
} from '@/components/map/entities'
import type { RiskLevel } from '@/api/types'
import { hazardLabel, groupUnlocatedByReason, riskLabel, riskLegend, UNLOCATED_LABELS } from '@/components/map/entities'
import type { NetworkStatus } from '@/components/map/offline'
import { NETWORK_LABELS, NETWORK_TAG_COLORS } from '@/components/map/offline'

const props = defineProps<{
  status: NetworkStatus
  statusReason: string
  basemapMessage: string
  terrainMessage: string
  counts: Record<LayerId, number>
  visibility: LayerVisibility
  unlocated: UnlocatedItem[]
  rejected: RejectedGeometry[]
  detail: FeatureDetail | null
  clustered: boolean
  zoom: number
  loading: boolean
  /** 三维场景起不来的原因（null 表示正常）；此时面板仍然可用。 */
  sceneError: string | null
  /** 风险等级筛选（空数组＝不过滤）。 */
  riskFilter: RiskLevel[]
  /** 各等级的要素数——芯片上的数字，也是图例的量化部分。 */
  riskCounts: Record<RiskLevel, number>
  /** 未筛选时的图层计数：筛选生效后行里要显示"筛后 / 全量"，不然数字看着像凭空少了。 */
  countsTotal: Record<LayerId, number>
}>()

const emit = defineEmits<{
  (event: 'toggle', layer: PlanLayer, visible: boolean): void
  (event: 'toggleRisk', level: RiskLevel): void
  (event: 'select', featureId: string): void
  (event: 'refresh'): void
}>()

const legend = riskLegend()

const LAYER_LABELS: Record<PlanLayer, string> = {
  stations: '监测站点',
  warnings: '预警落点',
  reach: '触达标记',
  hazards: '灾害分区面',
  clusters: '聚合计数',
}

/** 图层开关 + 要素计数（聚合层单独一栏，因为它不是一种数据而是画法）。 */
const layerRows = computed(() =>
  (['stations', 'warnings', 'reach', 'hazards'] as LayerId[]).map((layer) => ({
    layer,
    label: LAYER_LABELS[layer],
    count: props.counts[layer],
  })),
)

/** 筛选生效时把全量一起说出来：数字变小要能说清是"筛掉了"，不是"数据没了"。 */
function countText(layer: LayerId): string {
  const shown = props.counts[layer]
  return props.riskFilter.length === 0 ? String(shown) : `${shown} / ${props.countsTotal[layer]}`
}

/**
 * 芯片的三段颜色都由 `riskLegend()` 那一个色值算出来（同一份 `RISK_COLORS`，不另立第二套调色板）。
 *
 * 用 JS 拼 rgba 而不是 CSS `color-mix()`：自定义属性会原样收下不认识的函数，
 * 那时整条 `border` 声明会在计算值阶段失效——不如在这儿算干净，任何浏览器都是同一个结果。
 */
function chipStyle(hex: string): Record<string, string> {
  const rgb = /^#([\da-f]{2})([\da-f]{2})([\da-f]{2})$/i.exec(hex)?.slice(1)
  const [r, g, b] = (rgb ?? ['148', '163', '184']).map((pair) => Number.parseInt(pair, 16))
  return {
    '--chip': hex,
    '--chip-line': `rgba(${r}, ${g}, ${b}, 0.35)`,
    '--chip-wash': `rgba(${r}, ${g}, ${b}, 0.14)`,
    '--chip-halo': `rgba(${r}, ${g}, ${b}, 0.18)`,
  }
}

const station = computed(() => (props.detail?.kind === 'station' ? props.detail : null))
const warning = computed(() => (props.detail?.kind === 'warning' ? props.detail : null))
const reach = computed(() => (props.detail?.kind === 'reach' ? props.detail : null))
const zone = computed(() => (props.detail?.kind === 'hazard-zone' ? props.detail : null))
// 分组规则本身在 entities.ts（本组件不判断），这里只是把那份结果摆出来
const unlocatedGroups = computed(() => groupUnlocatedByReason(props.unlocated))

const detailTitle = computed(() => props.detail?.title ?? '')
const detailColor = computed(() => props.detail?.colorCss ?? '#8c8c8c')
const detailRisk = computed(() => riskLabel(props.detail?.riskLevel ?? null))
const detailHazard = computed(() => (props.detail ? hazardLabel(props.detail.hazardType) : ''))

function onToggle(layer: PlanLayer, visible: boolean): void {
  emit('toggle', layer, visible)
}
</script>

<template>
  <div class="panel">
    <a-card size="small" title="离线状态">
      <template #extra>
        <a-button size="small" :loading="props.loading" @click="emit('refresh')">刷新</a-button>
      </template>
      <a-space direction="vertical" size="small" style="width: 100%">
        <a-tag :color="NETWORK_TAG_COLORS[props.status]">
          {{ NETWORK_LABELS[props.status] }} · 缩放 {{ props.zoom }}
        </a-tag>
        <div class="note">{{ props.statusReason }}</div>
        <a-divider style="margin: 4px 0" />
        <div class="note">底图：{{ props.basemapMessage }}</div>
        <div class="note">地形：{{ props.terrainMessage }}</div>
        <a-alert
          v-if="props.sceneError"
          type="error"
          show-icon
          message="三维场景不可用"
          :description="props.sceneError"
        />
      </a-space>
    </a-card>

    <a-card size="small" title="图层" class="mt">
      <a-space direction="vertical" size="small" style="width: 100%">
        <div v-for="entry in layerRows" :key="entry.layer" class="row">
          <a-switch
            :checked="props.visibility[entry.layer]"
            size="small"
            @update:checked="(value: boolean) => onToggle(entry.layer, value)"
          />
          <span class="label">{{ entry.label }}</span>
          <span class="count">{{ countText(entry.layer) }}</span>
        </div>
        <div class="row">
          <a-switch
            :checked="props.visibility.clusters"
            size="small"
            @update:checked="(value: boolean) => onToggle('clusters', value)"
          />
          <span class="label">{{ LAYER_LABELS.clusters }}</span>
          <span class="count">{{ props.clustered ? '生效' : '未达阈值' }}</span>
        </div>
      </a-space>
      <a-divider style="margin: 8px 0" />
      <!-- 风险等级：一排芯片兼图例。颜色直接取 riskLegend()（与图上配色同一个 `RISK_COLORS`），
           未选中也保留该级 35% 描边 ⇒ 不点也能当图例读；点下去才是筛选。 -->
      <div class="risk-chips" role="group" aria-label="按风险等级筛选（颜色即图上配色）">
        <button
          v-for="entry in legend"
          :key="entry.level"
          type="button"
          class="risk-chip"
          :class="{ 'is-on': props.riskFilter.includes(entry.level) }"
          :aria-pressed="props.riskFilter.includes(entry.level)"
          :style="chipStyle(entry.colorCss)"
          :title="`${entry.label}：${props.riskCounts[entry.level]} 个要素${props.riskFilter.includes(entry.level) ? '（再点一下取消）' : '（点一下只看这一级）'}`"
          @click="emit('toggleRisk', entry.level)"
        >
          <span class="risk-chip__dot" />
          <span class="risk-chip__label">{{ entry.label }}</span>
          <span class="risk-chip__count">{{ props.riskCounts[entry.level] }}</span>
        </button>
      </div>
      <p v-if="props.riskFilter.length > 0" class="note">
        已按风险等级筛选，上面图层里的数字是筛后的（括弧内为全量）；没有评级的要素不受影响。
      </p>
      <span class="legend-item legend-item--static"><i style="background: #bfbfbf" /> 等级未知（不参与筛选）</span>
    </a-card>

    <a-card size="small" title="要素详情（点击图上要素）" class="mt">
      <a-empty v-if="!props.detail" description="未选中要素" />
      <a-descriptions v-else :column="1" bordered size="small" :title="detailTitle">
        <a-descriptions-item label="类型">{{ props.detail.kind }}</a-descriptions-item>
        <a-descriptions-item label="灾种">{{ detailHazard }}</a-descriptions-item>
        <a-descriptions-item label="风险">
          <a-tag :color="detailColor">{{ detailRisk }}</a-tag>
        </a-descriptions-item>
        <a-descriptions-item label="区域">{{ props.detail.regionCode || '—' }}</a-descriptions-item>

        <template v-if="station">
          <a-descriptions-item label="站点">{{ station.stationId }}</a-descriptions-item>
          <a-descriptions-item label="高程">{{ station.elevationM ?? '—' }} m</a-descriptions-item>
          <a-descriptions-item label="劣化读数">{{ station.suspectCount }} 条（不参与配色判定）</a-descriptions-item>
          <a-descriptions-item label="最新读数">
            <div v-for="sample in station.samples" :key="sample.metric" class="sample">
              {{ sample.metric }} = {{ sample.value }} {{ sample.unit }}
              <a-tag :color="sample.qualityFlag === 'ok' ? 'green' : 'orange'">{{ sample.qualityFlag }}</a-tag>
            </div>
            <span v-if="!station.samples.length">无遥测读数</span>
          </a-descriptions-item>
        </template>

        <template v-if="warning">
          <a-descriptions-item label="预警号">{{ warning.warningId }}</a-descriptions-item>
          <a-descriptions-item label="通道">{{ warning.channels.join(' / ') || '—' }}</a-descriptions-item>
          <a-descriptions-item label="生成/发布">
            {{ warning.generatedAt }} ／ {{ warning.releasedAt ?? '未发布' }}
          </a-descriptions-item>
          <a-descriptions-item label="触达">
            {{ warning.reach.delivered }}/{{ warning.reach.attempts }} 通道成功，覆盖
            {{ warning.reach.audienceCount }} 人
            <a-tag v-if="warning.reach.degraded" color="red">触达劣化</a-tag>
            <a-tag v-if="warning.translationPending" color="orange">藏文待译</a-tag>
          </a-descriptions-item>
        </template>

        <template v-if="reach">
          <a-descriptions-item label="覆盖人数">{{ reach.audienceCount }}</a-descriptions-item>
          <a-descriptions-item label="状态">{{ reach.degraded ? '存在失败/零送达通道' : '通道正常' }}</a-descriptions-item>
        </template>

        <template v-if="zone">
          <a-descriptions-item label="面顶点">{{ zone.outer.length }} 个（不含洞）</a-descriptions-item>
          <a-descriptions-item label="洞">{{ zone.holes.length }} 个</a-descriptions-item>
          <a-descriptions-item label="外接框">
            {{ zone.bbox.west.toFixed(3) }}, {{ zone.bbox.south.toFixed(3) }} →
            {{ zone.bbox.east.toFixed(3) }}, {{ zone.bbox.north.toFixed(3) }}
          </a-descriptions-item>
        </template>
      </a-descriptions>
    </a-card>

    <a-card size="small" class="mt">
      <template #title>
        未定位清单
        <a-tag v-if="props.unlocated.length" color="orange">{{ props.unlocated.length }}</a-tag>
      </template>
      <a-empty v-if="!props.unlocated.length" description="全部要素均已落到图上" />
      <!-- 光给一个总数答不了"我到底缺哪几样"：站点缺坐标与区域没烘焙锚点
           是两件要找不同的人办的事，混在一个数字里就都看不见了 -->
      <div v-if="unlocatedGroups.length" class="unlocated-groups" data-testid="unlocated-groups">
        <span v-for="group in unlocatedGroups" :key="group.reason" class="unlocated-group">
          {{ group.label }} × {{ group.count }}
        </span>
      </div>
      <!-- 上面那句"全部要素均已落到图上"已经说完了，这里再挂一个空列表就会叠出第二句
           antd 默认的"暂无数据"（真机 @1440 空数据档量到：同一块卡里两句并存，互相打脸）。
           所以列表只在真有内容时渲染。 -->
      <a-list v-else-if="props.unlocated.length > 0" size="small" :data-source="props.unlocated">
        <template #renderItem="{ item }">
          <a-list-item>
            <a-space direction="vertical" size="0" style="width: 100%">
              <span class="unlocated-title">{{ item.title }}</span>
              <span class="note">{{ UNLOCATED_LABELS[item.reason as UnlocatedReason] }}</span>
            </a-space>
            <template #actions>
              <a-button type="link" size="small" @click="emit('select', item.id)">详情</a-button>
            </template>
          </a-list-item>
        </template>
      </a-list>
      <div class="note tip">
        宁可列清单，也不把无坐标要素画到 (0,0)：一个位于几内亚湾的假站点会被误读成"高原没灾情"。
      </div>
    </a-card>

    <a-card v-if="props.rejected.length" size="small" title="未渲染的几何" class="mt">
      <a-list size="small" :data-source="props.rejected">
        <template #renderItem="{ item }">
          <a-list-item>
            <span class="note">#{{ item.index }} {{ item.id }} — {{ item.reason }}（{{ item.detail }}）</span>
          </a-list-item>
        </template>
      </a-list>
    </a-card>
  </div>
</template>

<style scoped>
.panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.mt {
  margin-top: 0;
}
.row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.row .label {
  flex: 1;
  font-size: 13px;
}
.row .count {
  font-variant-numeric: tabular-nums;
  color: #595959;
  font-size: 12px;
}
.note {
  font-size: 12px;
  color: #5a6072;
  line-height: 1.6;
}
.tip {
  margin-top: 8px;
}
.risk-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
/* 一排芯片兼图例：未选中也保留该级 35% 描边（`--chip-line` 由 riskLegend() 那一个色值算出来），
   选中才上底色。文字固定 #334155——把语义色当 12px 字用会掉到 AA 以下。 */
.risk-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 26px;
  padding: 3px 10px;
  border: 1px solid var(--chip-line);
  border-radius: 999px;
  background: transparent;
  color: #334155;
  font-size: 12px;
  font-weight: 600;
  line-height: 1.4;
  cursor: pointer;
  transition: background-color 0.16s ease, border-color 0.16s ease;
}
.risk-chip:hover {
  background: rgba(59, 130, 246, 0.06);
}
.risk-chip.is-on {
  border-color: var(--chip);
  background: var(--chip-wash);
}
.risk-chip:focus-visible {
  outline: 2px solid #2563eb;
  outline-offset: 2px;
}
.risk-chip__dot {
  width: 8px;
  height: 8px;
  flex: none;
  border-radius: 50%;
  background: var(--chip);
  box-shadow: 0 0 0 2px var(--chip-halo);
}
.risk-chip__count {
  font-variant-numeric: tabular-nums;
  color: #5a6072;
}
.legend-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: #595959;
}
.legend-item i {
  width: 10px;
  height: 10px;
  border-radius: 2px;
  display: inline-block;
}
.legend-item--static {
  margin-top: 6px;
}
.sample {
  font-size: 12px;
  line-height: 1.8;
}
.unlocated-groups {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 12px;
  margin-bottom: 8px;
  font-size: 12px;
  color: rgba(0, 0, 0, 0.65);
}
.unlocated-group {
  border-left: 2px solid #faad14;
  padding-left: 6px;
}
.unlocated-title {
  font-size: 13px;
}
</style>
