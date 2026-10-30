<script setup lang="ts">
/**
 * MapLayersPanel —— 「一张图」右列：图层开关 + 风险筛选 + 要素详情。
 *
 * 从原 `MapPanel.vue` 拆出来的右半（2026-10-30）。仍然只有 props/emits：
 * 不取数、不判断、不认识 Cesium。
 */

import { computed } from 'vue'

import type { FeatureDetail, LayerId, LayerVisibility, PlanLayer } from '@/components/map/entities'
import { hazardLabel, riskLabel, riskLegend } from '@/components/map/entities'
import type { RiskLevel } from '@/api/types'
import { chipStyle, countTextOf, LAYER_LABELS, layerRowsOf } from '@/components/map/panelParts'

const props = defineProps<{
  counts: Record<LayerId, number>
  visibility: LayerVisibility
  clustered: boolean
  /** 风险等级筛选（空数组＝不过滤）。 */
  riskFilter: RiskLevel[]
  /** 各等级的要素数——芯片上的数字，也是图例的量化部分。 */
  riskCounts: Record<RiskLevel, number>
  /** 未筛选时的图层计数：筛选生效后行里要显示"筛后 / 全量"，不然数字看着像凭空少了。 */
  countsTotal: Record<LayerId, number>
  detail: FeatureDetail | null
}>()

const emit = defineEmits<{
  (event: 'toggle', layer: PlanLayer, visible: boolean): void
  (event: 'toggleRisk', level: RiskLevel): void
}>()

const legend = riskLegend()
const layerRows = computed(() => layerRowsOf(props.counts))

function countText(layer: LayerId): string {
  return countTextOf(layer, props.counts, props.countsTotal, props.riskFilter.length)
}

function onToggle(layer: PlanLayer, visible: boolean): void {
  emit('toggle', layer, visible)
}

const station = computed(() => (props.detail?.kind === 'station' ? props.detail : null))
const warning = computed(() => (props.detail?.kind === 'warning' ? props.detail : null))
const reach = computed(() => (props.detail?.kind === 'reach' ? props.detail : null))
const zone = computed(() => (props.detail?.kind === 'hazard-zone' ? props.detail : null))

const detailTitle = computed(() => props.detail?.title ?? '')
const detailColor = computed(() => props.detail?.colorCss ?? '#8c8c8c')
const detailRisk = computed(() => riskLabel(props.detail?.riskLevel ?? null))
const detailHazard = computed(() => (props.detail ? hazardLabel(props.detail.hazardType) : ''))
</script>

<template>
  <div class="panel">
    <a-card size="small" title="图层">
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

    <a-card size="small" title="要素详情（点击图上要素）" class="detail-card">
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
      <!-- 收起/展开入口挂在"要素详情"卡的右下角（由 MapView 传入按钮） -->
      <slot name="detail-action" />
    </a-card>
  </div>
</template>

<style scoped>
.panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
/* 「要素详情」卡是收起按钮的定位锚点：按钮挂在右下角，body 留出一点底部空间 */
.detail-card {
  position: relative;
}
.detail-card :deep(.ant-card-body) {
  padding-bottom: 40px;
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
  color: var(--aeg-color-text-secondary);
  font-size: 12px;
}
.note {
  font-size: 12px;
  color: var(--aeg-color-text-secondary);
  line-height: 1.6;
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
  color: var(--aeg-color-text-secondary);
  font-size: 12px;
  font-weight: 600;
  line-height: 1.4;
  cursor: pointer;
  transition: background-color var(--aeg-motion-base) ease, border-color var(--aeg-motion-base) ease;
}
.risk-chip:hover {
  background: var(--aeg-color-brand-soft);
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
  color: var(--aeg-color-text-secondary);
}
.legend-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: var(--aeg-color-text-secondary);
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
</style>
