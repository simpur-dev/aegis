<script setup lang="ts">
/**
 * 态势总览的读数带：一张玻璃卡里四颗球。
 *
 * 2026-11-19 用户裁决：恢复球体（2dcec21 曾退役为紧凑统计格，用户验收后要求找回）。
 * - 最后一颗（协同成功率）是系统健康度的主指标，用紫色实心渐变球、直径大一号；
 *   判定靠 `index === items.length - 1` 而不是 label 字符串——DashboardView 的 kpis
 *   顺序就是业务顺序，改文案不该改视觉重点。
 * - 其余三颗是半透明白球；tone=warn/bad 时只把外圈光晕换成状态色，不整颗涂色
 *   （状态色只占边缘一小圈），名称前另留一颗语义小点（色觉冗余，2dcec21 的改进保留）。
 *
 * 两条诚实性规矩原样保留：取不到写 `—`（调用方算好值传进来）；有口径的数把口径写进 note。
 * `data-testid="stat-${label}"` 仍然只包数字 + 单位，`DashboardView.spec.ts` 靠它精确断言 "—个"。
 * 中文小字一律 12px（2dcec21 把 11px 提到 12px 的可读性改进保留，不回退）。
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
          <span class="kpi__value" :class="{ 'is-empty': item.value === '—' }">{{ item.value }}</span>
          <span v-if="item.suffix" class="kpi__suffix">{{ item.suffix }}</span>
        </div>
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
  /* 四颗球在卡片里均匀铺开：左右内缩相等，中间三处空隙由 space-between 均分 */
  justify-content: space-between;
  gap: 0;
  /* 卡片被拉伸到与右侧等高时，标题仍贴顶（与右侧标题齐），多出的高度落在标题与球之间 */
  align-items: flex-start;
  margin-bottom: var(--aeg-space-s4);
  padding: var(--aeg-space-s4);
  border: 1px solid var(--aeg-color-glass-border);
  border-radius: var(--aeg-radius-xl);
  background: var(--aeg-color-glass-bg);
  box-shadow: var(--aeg-shadow-md);
  /* 雾底已预模糊，面板级 blur 只付每帧重采样成本，不产生视觉差；
     合成层隔离用 translateZ(0) 自己留住，滚动搬纹理不重 raster */
  transform: translateZ(0);
  container-type: inline-size;
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
  /* 白球比紫球小一号：上边留 20px 让四颗球的"底"对齐，下面的标签才在同一水平线 */
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
/* 球里那颗"个/条"实测只有 2.87:1（深主题合成底 rgb(59,75,104) 上的 #8595AD，12px/600 要 4.5）。
   球面是全站最亮的一块，页级 muted 搬进来必然读不清——回到球自己的主字色，层级靠字号（12 vs 24）。 */
.kpi__ball .kpi__suffix {
  color: var(--aeg-color-text);
}
.kpi.is-accent .kpi__suffix {
  color: var(--aeg-color-accent-ball-text);
}
/* 空值的"灰掉"在紫球上换写法：深字压渐变亮端够大字号 3:1，降不透明度保留"没取到"的语义 */
.kpi.is-accent .kpi__value.is-empty {
  color: var(--aeg-color-accent-ball-text);
  opacity: 0.62;
}

.kpi__label {
  display: inline-flex;
  align-items: center;
  gap: var(--aeg-space-s1);
  font-size: var(--aeg-font-sm);
  font-weight: var(--aeg-weight-semibold);
  letter-spacing: 0.08em;
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

/* 卡片自身被挤窄时（例如助手抽屉拖宽）球体自动缩一档，避免四颗球换行；
   断点按卡片内容宽度算，不跟视口走——抽屉挤压不会触发 @media。 */
@container (max-width: 620px) {
  .kpi__ball {
    width: 112px;
    height: 112px;
  }
  .kpi.is-accent .kpi__ball {
    width: 132px;
    height: 132px;
  }
}
@container (max-width: 520px) {
  .kpi__ball {
    width: 96px;
    height: 96px;
    margin-top: var(--aeg-space-s4);
  }
  .kpi.is-accent .kpi__ball {
    width: 112px;
    height: 112px;
    margin-top: 0;
  }
}
</style>