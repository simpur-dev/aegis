<script setup lang="ts">
/**
 * 态势总览的读数带：四格紧凑统计格（数字在上、名称在下）。
 *
 * 2026-11-18 视觉裁决：球体退役。圆球把数字包在装饰里、无数据时是一颗紫渐变球摆"—"，
 * 首屏约 300px 高度花在球面而不是读数上。灾害防护界面的饱和色只留给预警四色与状态，
 * 读数格走墨色+纸白：大数字等宽、名称 12px、状态只占名称前一颗小点。
 *
 * 两条诚实性规矩原样保留：取不到写 `—`（调用方算好值传进来）；有口径的数把口径写进 note。
 * `data-testid="stat-${label}"` 仍然只包数字 + 单位，`DashboardView.spec.ts` 靠它精确断言 "—个"。
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
    <!-- 页头（标题/说明/更新时间/刷新）由调用方塞进来：它和四格读数同属"第一屏的那张卡" -->
    <div v-if="$slots.header" class="kpi-strip__header">
      <slot name="header" />
    </div>
    <div v-for="item in items" :key="item.label" class="kpi" :class="`is-${item.tone ?? 'idle'}`">
      <!-- 这一层只放数字 + 单位：测试靠它精确读出 "—个" / "137 条" -->
      <div class="kpi__row" :data-testid="`stat-${item.label}`">
        <span class="kpi__value" :class="{ 'is-empty': item.value === '—' }">{{ item.value }}</span>
        <span v-if="item.suffix" class="kpi__suffix">{{ item.suffix }}</span>
      </div>
      <div class="kpi__label">
        <span v-if="item.tone && item.tone !== 'idle'" class="kpi__dot" aria-hidden="true"></span>
        {{ item.label }}
      </div>
      <div v-if="item.note" class="kpi__note">{{ item.note }}</div>
    </div>
  </div>
</template>

<style scoped>
.kpi-strip {
  display: flex;
  flex-wrap: wrap;
  align-items: stretch;
  /* 行与行靠顶排：横幅在上、读数紧跟其下，不被拉伸到卡中部留下悬空数字 */
  align-content: flex-start;
  margin-bottom: var(--aeg-space-s4);
  padding: var(--aeg-space-s4);
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-lg);
  background: var(--aeg-color-card);
  box-shadow: var(--aeg-shadow-sm);
  container-type: inline-size;
}

/* 页头占满一整行，四格读数在它下面另起一行 */
.kpi-strip__header {
  flex: 1 0 100%;
  margin-bottom: var(--aeg-space-s3);
}

.kpi {
  display: flex;
  flex: 1 1 0;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--aeg-space-s1);
  min-width: 120px;
  padding: 0 var(--aeg-space-s4);
}
.kpi:first-of-type {
  padding-left: 0;
}
.kpi + .kpi {
  border-left: 1px solid var(--aeg-color-border-subtle);
}

.kpi__row {
  display: flex;
  align-items: baseline;
}
.kpi__value {
  font-family: var(--aeg-font-mono);
  font-variant-numeric: tabular-nums;
  font-size: var(--aeg-font-2xl);
  font-weight: var(--aeg-weight-bold);
  line-height: var(--aeg-lh-tight);
  color: var(--aeg-color-text);
}
/* 没取到就是没取到：灰掉，不拿装饰球把它扮成"一个状态" */
.kpi__value.is-empty {
  color: var(--aeg-color-text-muted);
}
.kpi__suffix {
  margin-left: var(--aeg-space-s1);
  font-size: var(--aeg-font-sm);
  font-weight: var(--aeg-weight-semibold);
  color: var(--aeg-color-text-muted);
}

.kpi__label {
  display: inline-flex;
  align-items: center;
  gap: var(--aeg-space-s1);
  font-size: var(--aeg-font-sm);
  font-weight: var(--aeg-weight-semibold);
  color: var(--aeg-color-text-secondary);
}
.kpi__dot {
  width: 8px;
  height: 8px;
  border-radius: var(--aeg-radius-pill);
  background: var(--aeg-color-border-strong);
}
.kpi.is-ok .kpi__dot {
  background: var(--aeg-color-status-success);
}
.kpi.is-warn .kpi__dot {
  background: var(--aeg-color-status-warning);
}
.kpi.is-bad .kpi__dot {
  background: var(--aeg-color-status-danger);
}
.kpi__note {
  font-size: var(--aeg-font-sm);
  color: var(--aeg-color-text-muted);
  line-height: var(--aeg-lh-snug);
}

/* 窄屏与抽屉挤压：2×2，分隔线只留竖的那条 */
@container (max-width: 620px) {
  .kpi {
    flex: 1 1 40%;
    padding: 0 var(--aeg-space-s3);
  }
  .kpi:nth-of-type(odd) {
    padding-left: 0;
    border-left: none;
  }
  .kpi:nth-of-type(n + 3) {
    margin-top: var(--aeg-space-s3);
  }
}
</style>