<script setup lang="ts">
import { AppstoreOutlined, BellOutlined, PartitionOutlined, RobotOutlined } from '@ant-design/icons-vue'

const icons = {
  agents: RobotOutlined,
  warnings: BellOutlined,
  tasks: AppstoreOutlined,
  collaboration: PartitionOutlined,
}

export interface KpiItem {
  metric: keyof typeof icons
  label: string
  value: string | number
  suffix?: string
  note?: string
  tone?: 'ok' | 'warn' | 'bad' | 'idle'
}

defineProps<{ items: KpiItem[] }>()
</script>

<template>
  <ul class="kpi-strip" aria-label="平台实时指标">
    <li
      v-for="item in items"
      :key="item.metric"
      class="kpi"
      :class="[`is-${item.tone ?? 'idle'}`, `kpi--${item.metric}`, { 'is-accent': item.metric === 'collaboration' }]"
    >
      <div class="kpi__icon" aria-hidden="true"><component :is="icons[item.metric]" /></div>
      <dl class="kpi__reading">
        <dt class="kpi__label">
          <span v-if="item.tone && item.tone !== 'idle'" class="kpi__dot" aria-hidden="true"></span>
          {{ item.label }}
        </dt>
        <dd class="kpi__row" :data-testid="`stat-${item.label}`">
          <span class="kpi__value">{{ item.value }}</span>
          <span v-if="item.suffix" class="kpi__suffix">{{ item.suffix }}</span>
        </dd>
        <dd v-if="item.note" class="kpi__note">{{ item.note }}</dd>
      </dl>
    </li>
  </ul>
</template>

<style scoped>
.kpi-strip {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: var(--aeg-space-s3);
  margin: 0;
  padding: 0;
  list-style: none;
}
.kpi {
  --kpi-accent: var(--aeg-color-overview-accent);
  display: flex;
  align-items: center;
  gap: var(--aeg-space-s2);
  min-height: 76px;
  padding: var(--aeg-space-s2);
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-md);
  background: var(--aeg-color-overview-tile);
  box-shadow: var(--aeg-shadow-sm);
}
.kpi--warnings { --kpi-accent: var(--aeg-tag-orange-text); }
.kpi--tasks { --kpi-accent: var(--aeg-color-status-info); }
.kpi--collaboration { --kpi-accent: var(--aeg-color-editor-accent-teal); }
html[data-theme='dark'] .kpi {
  border-color: transparent;
  background:
    radial-gradient(ellipse at 1% 3%, color-mix(in srgb, var(--kpi-accent) 29%, transparent), transparent 68%) padding-box,
    radial-gradient(ellipse 70% 50% at 90% 105%, color-mix(in srgb, var(--kpi-accent) 14%, transparent), transparent) padding-box,
    var(--dash-detail-face) padding-box,
    radial-gradient(ellipse 88% 74% at 2% 0%, color-mix(in srgb, var(--kpi-accent) 59%, var(--aeg-color-command-line)),
      var(--aeg-color-command-line)) border-box;
}
html[data-theme='light'] .kpi.is-accent {
  border-color: color-mix(in srgb, var(--aeg-color-overview-accent), var(--aeg-color-border) 68%);
}
.kpi__icon {
  display: grid;
  flex: 0 0 36px;
  place-items: center;
  height: 36px;
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-md);
  background: var(--aeg-color-sunken);
  color: var(--aeg-color-overview-accent);
  font-size: var(--aeg-font-lg);
}
html[data-theme='dark'] .kpi .kpi__icon {
  border-color: color-mix(in srgb, var(--kpi-accent) 44%, transparent);
  border-radius: 50%;
  background: color-mix(in srgb, var(--kpi-accent) 24%, var(--aeg-color-command-deep));
  color: var(--kpi-accent);
  box-shadow: 0 2px 10px color-mix(in srgb, var(--kpi-accent) 20%, transparent);
}
.kpi.is-accent .kpi__icon {
  background: var(--aeg-color-brand-soft);
}
.kpi--warnings .kpi__icon {
  color: var(--aeg-tag-orange-text);
}
.kpi--tasks .kpi__icon {
  color: var(--aeg-color-brand-alt);
}
.kpi--collaboration.is-ok .kpi__icon {
  color: var(--aeg-color-status-success);
}
.kpi__reading {
  display: flex;
  flex-direction: column;
  min-width: 0;
  margin: 0;
}
.kpi__row {
  order: -1;
  display: flex;
  align-items: baseline;
  gap: var(--aeg-space-s1);
  margin: 0;
}
.kpi__value {
  color: var(--aeg-color-text);
  font: var(--aeg-weight-bold) clamp(var(--aeg-font-lg), 1.7vw, var(--aeg-font-2xl)) / var(--aeg-lh-tight) var(--aeg-font-mono);
  letter-spacing: -0.035em;
}
.kpi__suffix,
.kpi__label {
  color: var(--aeg-color-text-secondary);
  font-size: var(--aeg-font-sm);
  font-weight: var(--aeg-weight-semibold);
}
.kpi__label {
  display: flex;
  align-items: center;
  gap: var(--aeg-space-s1);
}
.kpi__dot {
  flex: 0 0 7px;
  height: 7px;
  border-radius: 50%;
  background: currentColor;
}
.kpi__note {
  margin: 0;
  color: var(--aeg-color-text-secondary);
  font-size: var(--aeg-font-sm);
  line-height: var(--aeg-lh-snug);
}
.kpi.is-ok :is(.kpi__dot, .kpi__note) {
  color: var(--aeg-color-status-success);
}
.kpi.is-warn :is(.kpi__dot, .kpi__note) {
  color: var(--aeg-color-status-warning);
}
.kpi.is-bad :is(.kpi__dot, .kpi__note) {
  color: var(--aeg-color-status-danger);
}
@container dashboard (max-width: 660px) {
  .kpi-strip {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: var(--aeg-space-s2);
  }
}
@container dashboard (max-width: 340px) {
  .kpi__icon {
    display: none;
  }
}
</style>
