<script setup lang="ts">
import { useId } from 'vue'

defineProps<{ value: number | null }>()

const gradient = `metric-ring-${useId()}`
</script>

<template>
  <div class="metric-ring">
    <svg class="metric-ring__dial" viewBox="0 0 100 100" aria-hidden="true">
      <defs>
        <linearGradient :id="gradient" x1="0" x2="1" y1="0" y2="1">
          <stop class="metric-ring__highlight" />
          <stop offset="1" stop-color="currentColor" />
        </linearGradient>
      </defs>
      <circle class="metric-ring__track" cx="50" cy="50" r="44" />
      <circle
        v-if="value !== null && value > 0"
        cx="50"
        cy="50"
        r="44"
        pathLength="100"
        :stroke="'url(#' + gradient + ')'"
        :stroke-dasharray="`${Math.min(value, 100)} 100`"
        transform="rotate(-90 50 50)"
      />
    </svg>
    <div class="metric-ring__readout"><slot /></div>
  </div>
</template>

<style scoped>
.metric-ring {
  display: grid;
  width: var(--metrics-dial-size, 96px);
  max-width: 100%;
  aspect-ratio: 1;
  flex: none;
  place-items: center;
  color: var(--metrics-status, var(--aeg-color-status-success));
}
.metric-ring > * { grid-area: 1 / 1; }
.metric-ring__dial { width: 100%; overflow: visible; }
.metric-ring__dial circle { fill: none; stroke-width: 10; }
.metric-ring__track { stroke: color-mix(in srgb, currentColor 16%, var(--aeg-color-border)); }
.metric-ring__highlight { stop-color: color-mix(in srgb, currentColor 62%, white); }
.metric-ring__readout { text-align: center; color: var(--aeg-color-text); }
</style>
