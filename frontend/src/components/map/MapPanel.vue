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
import { hazardLabel, riskLabel, riskLegend, UNLOCATED_LABELS } from '@/components/map/entities'
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
}>()

const emit = defineEmits<{
  (event: 'toggle', layer: PlanLayer, visible: boolean): void
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

const station = computed(() => (props.detail?.kind === 'station' ? props.detail : null))
const warning = computed(() => (props.detail?.kind === 'warning' ? props.detail : null))
const reach = computed(() => (props.detail?.kind === 'reach' ? props.detail : null))
const zone = computed(() => (props.detail?.kind === 'hazard-zone' ? props.detail : null))

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
          <span class="count">{{ entry.count }}</span>
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
      <div class="legend">
        <span v-for="entry in legend" :key="entry.level" class="legend-item">
          <i :style="{ background: entry.colorCss }" />
          {{ entry.label }}
        </span>
        <span class="legend-item"><i style="background: #bfbfbf" /> 等级未知</span>
      </div>
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
      <a-list v-else size="small" :data-source="props.unlocated">
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
  color: #8c8c8c;
  line-height: 1.6;
}
.tip {
  margin-top: 8px;
}
.legend {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 12px;
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
.sample {
  font-size: 12px;
  line-height: 1.8;
}
.unlocated-title {
  font-size: 13px;
}
</style>
