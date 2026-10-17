<script setup lang="ts">
/**
 * 态势总览的读数带（spike：dashboard 标杆页）。
 *
 * 为什么从 PageHero 的数字托盘里撤出来：托盘是"页头的附属品"，视觉权重与一句 caption 相当，
 * 而值班员第一眼要抓的恰恰是这四个数（现场规模）。独立成带之后数字给到 2xl 等宽，
 * 标签降为小字眉——读数时眼睛先抓大数，再找它是什么。
 *
 * 两条诚实性规矩与旧托盘一致，只是换了宿主：
 * ① 取不到写 `—`，不拿 0 冒充"没有"（调用方把 value 算好传进来）；
 * ② 有口径的数（协同成功率）把口径写进 note，不写进 suffix——suffix 只放单位。
 */
export interface KpiItem {
  label: string
  value: string | number
  suffix?: string
  /** 口径注脚（"目标 ≥95% · 达标"这类），只写与读数有关的实话。 */
  note?: string
  /** 有含义才给色：达标绿 / 越限红 / 警告橙；不给就是正文色。 */
  tone?: 'ok' | 'warn' | 'bad' | 'idle'
}

defineProps<{ items: KpiItem[] }>()
</script>

<template>
  <div class="kpi-strip">
    <!-- data-testid 钉在读数行上而不是整格：整格会把标签名也算进 text()，
         "—个" 这类精确断言就连标签一起读到了。 -->
    <div v-for="item in items" :key="item.label" class="kpi" :class="`is-${item.tone ?? 'idle'}`">
      <div class="kpi__label">{{ item.label }}</div>
      <div class="kpi__row" :data-testid="`stat-${item.label}`"><span class="kpi__value">{{ item.value }}</span><span v-if="item.suffix" class="kpi__suffix">{{ item.suffix }}</span></div>
      <div v-if="item.note" class="kpi__note">{{ item.note }}</div>
    </div>
  </div>
</template>

<style scoped>
.kpi-strip {
  display: grid;
  /* 200px 下限：1366 一档四格各 ~250px，窄档自动折成 2×2，不用媒体查询 */
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: var(--aeg-space-s3);
  margin-bottom: var(--aeg-space-s3);
}
.kpi {
  padding: var(--aeg-space-s3) var(--aeg-space-s4);
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-md);
  background: var(--aeg-color-card);
  box-shadow: var(--aeg-shadow-sm);
}
.kpi__label {
  font-size: var(--aeg-font-xs);
  font-weight: var(--aeg-weight-semibold);
  letter-spacing: 0.08em;
  color: var(--aeg-color-text-muted);
}
.kpi__row {
  display: flex;
  align-items: baseline;
  margin-top: var(--aeg-space-s1);
}
.kpi__value {
  font-family: var(--aeg-font-mono);
  font-variant-numeric: tabular-nums;
  font-size: var(--aeg-font-2xl);
  font-weight: var(--aeg-weight-bold);
  line-height: var(--aeg-lh-tight);
  color: var(--aeg-color-text);
}
.kpi.is-ok .kpi__value {
  color: var(--aeg-color-status-success);
}
.kpi.is-warn .kpi__value {
  color: var(--aeg-color-status-warning);
}
.kpi.is-bad .kpi__value {
  color: var(--aeg-color-status-danger);
}
.kpi__suffix {
  margin-left: var(--aeg-space-s1);
  font-size: var(--aeg-font-sm);
  font-weight: var(--aeg-weight-semibold);
  color: var(--aeg-color-text-muted);
}
.kpi__note {
  margin-top: var(--aeg-space-s1);
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-text-muted);
}
</style>
