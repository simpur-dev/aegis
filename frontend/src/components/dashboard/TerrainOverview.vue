<script setup lang="ts">
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
const legend = [1, 2, 3, 4, 5] as const
</script>

<template>
  <section class="terrain-preview" aria-label="风险概览" data-testid="dashboard-terrain">
    <img class="terrain-preview__image" src="/backgrounds/tibet-relief.webp" alt="" width="2124" height="740" />
    <header class="terrain-preview__head">
      <span class="terrain-preview__pill">风险区域 <strong>{{ hasSnapshot ? riskRegions.length : '—' }}</strong></span>
      <router-link class="terrain-preview__pill terrain-preview__link" to="/map">查看一张图 <span aria-hidden="true">→</span></router-link>
    </header>
    <footer class="terrain-preview__bottom">
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
      <div class="terrain-preview__key">
        <ul class="terrain-preview__pill terrain-preview__legend" aria-label="风险等级图例">
          <li v-for="level in legend" :key="level" :style="{ '--risk-color': RISK_COLORS[level] }">
            <span class="terrain-preview__dot" aria-hidden="true"></span>
            {{ RISK_LABELS[level] }}
          </li>
        </ul>
        <small class="terrain-preview__pill">地形示意</small>
      </div>
    </footer>
  </section>
</template>

<style scoped>
.terrain-preview {
  position: relative;
  isolation: isolate;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  gap: var(--aeg-space-s4);
  min-height: clamp(212px, calc(46vh - 160px), 340px);
  padding: var(--aeg-space-s3);
  color: var(--aeg-color-terrain-ink);
  background: var(--aeg-color-terrain-bg);
}
.terrain-preview__image {
  position: absolute;
  z-index: -1;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
  object-position: center 53%;
}
.terrain-preview__head,
.terrain-preview__key {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  align-items: center;
  gap: var(--aeg-space-s2);
}
.terrain-preview__bottom {
  display: grid;
  gap: var(--aeg-space-s2);
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
  padding-inline: var(--aeg-space-s3);
  min-height: 32px;
  font-weight: var(--aeg-weight-semibold);
}
.terrain-preview__link:hover,
.terrain-preview__link:focus-visible {
  color: var(--aeg-color-terrain-accent);
  border-color: currentColor;
}
.terrain-preview__regions,
.terrain-preview__legend {
  display: flex;
  flex-wrap: wrap;
  gap: var(--aeg-space-s2);
  margin: 0;
  list-style: none;
}
.terrain-preview__regions {
  padding: 0;
}
.terrain-preview__region {
  border-color: color-mix(in srgb, var(--risk-color), var(--aeg-color-terrain-bg) 36%);
  cursor: pointer;
  font-family: inherit;
}
.terrain-preview__region:hover,
.terrain-preview__region[aria-pressed='true'] {
  border-color: var(--risk-color);
}
.terrain-preview__legend li {
  display: flex;
  align-items: center;
  gap: var(--aeg-space-s1);
  white-space: nowrap;
}
.terrain-preview__dot {
  flex: 0 0 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--risk-color);
}
.terrain-preview__mock {
  color: var(--aeg-color-terrain-accent);
}
@media (max-width: 520px) {
  .terrain-preview {
    min-height: 256px;
  }
}
</style>
