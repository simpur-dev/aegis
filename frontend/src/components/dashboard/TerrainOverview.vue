<script setup lang="ts">
import { ArrowRightOutlined, EnvironmentOutlined, GlobalOutlined } from '@ant-design/icons-vue'
import { computed } from 'vue'

import { RISK_COLORS, RISK_LABELS } from '@/api/types'
import type { RiskCell } from './RiskWall.vue'

const props = defineProps<{ cells: RiskCell[]; hasSnapshot: boolean; selectedId: string | null }>()
const emit = defineEmits<{ select: [region: string] }>()

const riskRegions = computed(() => props.cells.filter((cell) => cell.level < 5 && cell.region !== '未标注区域'))
const featuredRegions = computed(() => {
  const priority = riskRegions.value.slice(0, 3)
  const selected = riskRegions.value.find((cell) => cell.region === props.selectedId)
  return selected && !priority.includes(selected) ? [...priority.slice(0, 2), selected] : priority
})
const levels = [1, 2, 3, 4, 5] as const
const legend = computed(() => levels.map((level) => ({
  level,
  count: riskRegions.value.filter((region) => region.level === level).length,
})))
</script>

<template>
  <section class="terrain-preview" aria-label="风险概览" data-testid="dashboard-terrain">
    <img class="terrain-preview__image" src="/backgrounds/tibet-relief.webp" alt="" width="2124" height="740" />
    <header class="terrain-preview__head">
      <span class="terrain-preview__pill"><EnvironmentOutlined aria-hidden="true" /> 风险区域 <strong>{{ hasSnapshot ? riskRegions.length : '—' }}</strong></span>
      <small class="terrain-preview__credit">地形示意</small>
    </header>
    <ul v-if="riskRegions.length" class="terrain-preview__regions" aria-label="优先关注的区域">
      <li v-for="region in featuredRegions" :key="region.region">
        <button
          type="button"
          class="terrain-preview__pill terrain-preview__region"
          :style="{ '--risk-color': RISK_COLORS[region.level] }"
          :aria-pressed="selectedId === region.region"
          @click="emit('select', region.region)"
        >
          <span class="terrain-preview__dot" aria-hidden="true"></span>
          <strong>{{ region.region }} · {{ region.hazard }}</strong>
          <span>{{ RISK_LABELS[region.level] }}</span>
          <span v-if="region.isMock" class="terrain-preview__mock">模拟</span>
        </button>
      </li>
    </ul>
    <aside class="terrain-preview__key" aria-label="风险等级图例">
      <h2><GlobalOutlined aria-hidden="true" /> 山地灾害风险态势</h2>
      <ul class="terrain-preview__legend">
        <li v-for="entry in legend" :key="entry.level" :style="{ '--risk-color': RISK_COLORS[entry.level] }">
          <span class="terrain-preview__dot" aria-hidden="true"></span>
          <span>{{ RISK_LABELS[entry.level] }}</span>
          <span v-if="entry.level < 5" class="terrain-preview__count">{{ hasSnapshot ? entry.count : '—' }}</span>
        </li>
      </ul>
      <router-link class="terrain-preview__pill terrain-preview__link" to="/map">
        查看一张图 <ArrowRightOutlined aria-hidden="true" />
      </router-link>
    </aside>
  </section>
</template>

<style scoped>
.terrain-preview {
  position: relative;
  isolation: isolate;
  display: grid;
  grid-template: 'head key' 1fr 'regions key' auto / minmax(0, 1fr) auto;
  align-items: start;
  gap: var(--aeg-space-s2) var(--aeg-space-s3);
  min-height: clamp(216px, calc(49vh - 158px), 390px);
  padding: var(--aeg-space-s3);
  color: var(--aeg-color-terrain-ink);
  background: var(--aeg-color-terrain-bg);
}
.terrain-preview::before {
  content: '';
  position: absolute;
  inset: 0;
  z-index: -1;
  background: linear-gradient(180deg, var(--aeg-color-terrain-bg), transparent 24%, transparent 60%, var(--aeg-color-terrain-bg)),
    linear-gradient(90deg, transparent 65%, color-mix(in srgb, var(--aeg-color-terrain-bg) 76%, transparent));
  opacity: 0.82;
}
html[data-theme='dark'] .terrain-preview::after {
  content: '';
  position: absolute;
  z-index: -1;
  inset: 0;
  box-shadow: inset 0 -12px 18px -12px var(--aeg-color-command-glow);
  pointer-events: none;
}
.terrain-preview__image {
  position: absolute;
  z-index: -2;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
  object-position: center 50%;
}
.terrain-preview__head {
  grid-area: head;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--aeg-space-s2);
}
.terrain-preview__credit {
  color: var(--aeg-color-terrain-ink);
  padding: 2px var(--aeg-space-s1);
  background: var(--aeg-color-terrain-bg);
  border-radius: var(--aeg-radius-sm);
}
.terrain-preview__key {
  grid-area: key;
  align-self: center;
  display: grid;
  gap: var(--aeg-space-s2);
  width: 154px;
  padding: var(--aeg-space-s2);
  border: 1px solid var(--aeg-color-terrain-line);
  border-radius: var(--aeg-radius-md);
  background: linear-gradient(150deg, color-mix(in srgb, var(--aeg-color-terrain-line) 36%, var(--aeg-color-terrain-bg)), var(--aeg-color-terrain-bg));
}
html[data-theme='dark'] .terrain-preview__key {
  border-color: transparent;
  background: var(--dash-detail-face) padding-box, var(--dash-detail-rim) border-box;
  box-shadow: 0 4px 16px var(--aeg-color-overlay-scrim);
}
.terrain-preview__key h2 {
  display: flex;
  align-items: center;
  gap: var(--aeg-space-s1);
  margin: 0;
  padding-bottom: var(--aeg-space-s1);
  border-bottom: 1px solid var(--aeg-color-terrain-line);
  color: var(--aeg-color-terrain-ink);
  font-size: var(--aeg-font-sm);
  font-weight: var(--aeg-weight-bold);
}
.terrain-preview__key h2 > :first-child,
.terrain-preview__head .terrain-preview__pill > :first-child {
  color: var(--aeg-color-terrain-accent);
}
.terrain-preview__pill {
  display: inline-flex;
  align-items: center;
  gap: var(--aeg-space-s1);
  max-width: 100%;
  padding: var(--aeg-space-s1) var(--aeg-space-s2);
  border: 1px solid var(--aeg-color-terrain-line);
  border-radius: var(--aeg-radius-pill);
  background: var(--aeg-color-terrain-bg);
  color: inherit;
  font-size: var(--aeg-font-sm);
  line-height: var(--aeg-lh-snug);
}
.terrain-preview__link {
  justify-content: space-between;
  padding-inline: var(--aeg-space-s2);
  min-height: 32px;
  font-weight: var(--aeg-weight-semibold);
}
.terrain-preview__link:hover,
.terrain-preview__link:focus-visible {
  color: var(--aeg-color-terrain-accent);
  border-color: currentColor;
}
.terrain-preview__regions {
  grid-area: regions;
  display: flex;
  flex-wrap: wrap;
  gap: var(--aeg-space-s2);
  margin: 0;
  list-style: none;
  padding: 0;
}
.terrain-preview__legend {
  display: grid;
  gap: var(--aeg-space-s1);
  padding: 0;
  margin: 0;
  list-style: none;
  font-size: var(--aeg-font-sm);
}
.terrain-preview__region {
  border-color: color-mix(in srgb, var(--risk-color) 75%, var(--aeg-color-terrain-ink));
  background: color-mix(in srgb, var(--risk-color) 18%, var(--aeg-color-terrain-bg));
  cursor: pointer;
  font-family: inherit;
}
.terrain-preview__region:hover,
.terrain-preview__region[aria-pressed='true'] {
  border-color: var(--risk-color);
}
.terrain-preview__legend li {
  display: grid;
  grid-template-columns: 8px minmax(0, 1fr) auto;
  align-items: center;
  gap: var(--aeg-space-s1);
  white-space: nowrap;
}
.terrain-preview__count {
  font-variant-numeric: tabular-nums;
  font-weight: var(--aeg-weight-bold);
}
.terrain-preview__dot {
  flex: 0 0 8px;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--risk-color);
  box-shadow: 0 0 8px color-mix(in srgb, var(--risk-color) 55%, transparent);
}
.terrain-preview__mock {
  color: var(--aeg-color-terrain-accent);
}
@container dashboard (max-width: 520px) {
  .terrain-preview {
    grid-template: 'head' auto 'regions' 1fr 'key' auto / minmax(0, 1fr);
    min-height: 300px;
  }
  .terrain-preview__image {
    object-fit: contain;
    object-position: center 25%;
  }
  .terrain-preview__regions {
    align-self: end;
  }
  .terrain-preview__key {
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    width: auto;
  }
  .terrain-preview__key h2 {
    grid-column: 1 / -1;
  }
  .terrain-preview__legend {
    display: flex;
    flex-wrap: wrap;
  }
}
</style>
