<script setup lang="ts">
/**
 * 态势总览的读数带（spike：dashboard 顶部的四个球）。
 *
 * 2026-10-25 玻璃拟态改版：从「四张并排白卡」改成「一张玻璃卡里四颗球」。
 * - 最后一颗（协同成功率）是系统健康度的主指标，用紫色实心渐变球、直径大一号；
 *   判定靠 `index === items.length - 1` 而不是 label 字符串——DashboardView 的 kpis
 *   顺序就是业务顺序，改文案不该改视觉重点。
 * - 其余三颗是半透明白球；tone=warn/bad 时只把外圈光晕换成状态色，不整颗涂色
 *   （状态色只占边缘一小圈）。
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
    <!-- 页头（标题/说明/更新时间/刷新）由调用方塞进来：它和四颗球同属"第一屏的那张卡" -->
    <div v-if="$slots.header" class="kpi-strip__header">
      <slot name="header" />
    </div>
    <div
      v-for="(item, index) in items"
      :key="item.label"
      class="kpi"
      :class="[`is-${item.tone ?? 'idle'}`, { 'is-accent': index === items.length - 1 }]"
    >
      <div class="kpi__ball">
        <!-- 这一层只放数字 + 单位：测试靠它精确读出 "—个" / "137 条" -->
        <div class="kpi__row" :data-testid="`stat-${item.label}`">
          <span class="kpi__value">{{ item.value }}</span>
          <span v-if="item.suffix" class="kpi__suffix">{{ item.suffix }}</span>
        </div>
      </div>
      <div class="kpi__label">{{ item.label }}</div>
      <div v-if="item.note" class="kpi__note">{{ item.note }}</div>
    </div>
  </div>
</template>

<style scoped>
.kpi-strip {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-start;
  /* 四颗球"挨着站"：间距压到 4px，像参考图里白球-紫球-白球那样成组，而不是四张卡摊满一行 */
  gap: var(--aeg-space-s1);
  align-items: flex-start;
  margin-bottom: var(--aeg-space-s4);
  padding: var(--aeg-space-s4) var(--aeg-space-s5);
  border: 1px solid var(--aeg-color-glass-border);
  border-radius: var(--aeg-radius-xl);
  background: var(--aeg-color-glass-bg);
  box-shadow: var(--aeg-shadow-md);
  backdrop-filter: blur(var(--aeg-glass-blur));
}

/* 页头占满一整行，四颗球在它下面另起一行 */
.kpi-strip__header {
  flex: 1 0 100%;
  margin-bottom: var(--aeg-space-s2);
}

.kpi {
  display: flex;
  flex: 0 0 auto;
  flex-direction: column;
  align-items: center;
  gap: var(--aeg-space-s2);
  min-width: 128px;
  text-align: center;
}

/* 球：白球 = 顶部高光 + 细描边 + 很淡的外投影；紫球 = 实心径向渐变，直径大一号。 */
.kpi__ball {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 128px;
  height: 128px;
  /* 白球比紫球小一号：上边留 24px 让四颗球的"底"对齐，下面的标签才在同一水平线 */
  margin-top: var(--aeg-space-s5);
  border: 1px solid var(--aeg-color-ball-ring);
  border-radius: 50%;
  background: radial-gradient(circle at 32% 26%, var(--aeg-color-ball-ring), var(--aeg-color-ball-surface) 68%);
  box-shadow:
    inset 0 1px 0 var(--aeg-color-ball-ring),
    0 18px 32px -20px var(--aeg-color-accent-ball-glow);
}

.kpi.is-accent .kpi__ball {
  width: 152px;
  height: 152px;
  margin-top: 0;
  border-color: var(--aeg-color-accent-ball-from);
  background: radial-gradient(circle at 32% 26%, var(--aeg-color-accent-ball-from), var(--aeg-color-accent-ball-to) 70%);
  box-shadow:
    inset 0 2px 14px var(--aeg-color-glass-border),
    0 26px 48px -22px var(--aeg-color-accent-ball-glow);
}

/* 状态色只上外圈：warn/bad 时把描边与外圈光晕换成对应语义色，球体本身仍是白的。 */
.kpi.is-warn .kpi__ball {
  border-color: var(--aeg-color-status-warning);
  box-shadow:
    inset 0 1px 0 var(--aeg-color-ball-ring),
    0 0 0 3px var(--aeg-tag-orange-bg),
    0 18px 32px -20px var(--aeg-color-accent-ball-glow);
}
.kpi.is-bad .kpi__ball {
  border-color: var(--aeg-color-status-danger);
  box-shadow:
    inset 0 1px 0 var(--aeg-color-ball-ring),
    0 0 0 3px var(--aeg-tag-red-bg),
    0 18px 32px -20px var(--aeg-color-accent-ball-glow);
}
.kpi.is-ok .kpi__ball {
  border-color: var(--aeg-color-status-success);
}

.kpi__row {
  display: flex;
  align-items: baseline;
  justify-content: center;
}
.kpi__value {
  font-family: var(--aeg-font-mono);
  font-variant-numeric: tabular-nums;
  font-size: var(--aeg-font-xl);
  font-weight: var(--aeg-weight-bold);
  line-height: var(--aeg-lh-tight);
  color: var(--aeg-color-text);
}
.kpi.is-accent .kpi__value {
  font-size: var(--aeg-font-2xl);
  color: var(--aeg-color-accent-ball-text);
}
.kpi__suffix {
  margin-left: var(--aeg-space-s1);
  font-size: var(--aeg-font-xs);
  font-weight: var(--aeg-weight-semibold);
  color: var(--aeg-color-text-muted);
}
.kpi.is-accent .kpi__suffix {
  color: var(--aeg-color-accent-ball-text);
}

.kpi__label {
  font-size: var(--aeg-font-xs);
  font-weight: var(--aeg-weight-semibold);
  letter-spacing: 0.08em;
  color: var(--aeg-color-text-secondary);
}
.kpi__note {
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-text-muted);
  line-height: var(--aeg-lh-snug);
}

@media (max-width: 900px) {
  .kpi-strip {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
