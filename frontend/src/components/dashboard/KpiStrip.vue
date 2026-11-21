<script setup lang="ts">
/**
 * 态势总览的读数带：一排横向数据卡（图标 + 数字 + 单位 + 名称 + 趋势）。
 *
 * 2026-11-20 指挥中心裁决：统计球退役（任务书明确"取消统计球"），换成现代横向指标卡。
 * - 四张卡同一套图标语言：线性 SVG、统一青蓝图标底，状态色只上语义小点与趋势箭头，
 *   不整卡涂色；最后一张（协同成功率）是主指标，按业务顺序（index === items.length - 1）
 *   给一圈品牌描边——改文案不该改视觉重点。
 * - 趋势只写量得到的：调用方拿两次轮询的真实差值算 `trend`，没有差值就落口径注脚 `note`，
 *   绝不编一个"较昨日 +12%"。
 *
 * 两条诚实性规矩原样保留：取不到写 `—`（调用方算好值传进来）；有口径的数把口径写进 note。
 * `data-testid="stat-${label}"` 仍然只包数字 + 单位，`DashboardView.spec.ts` 靠它精确断言 "—个"。
 * 中文小字一律 12px。
 */
export interface KpiItem {
  label: string
  value: string | number
  suffix?: string
  /** 口径注脚（"目标 ≥95% · 达标"这类），只写与读数有关的实话。 */
  note?: string
  /** 有含义才给色：达标绿 / 越限红 / 警告橙；不给就是正文色。 */
  tone?: 'ok' | 'warn' | 'bad' | 'idle'
  /** 真实两次取数的差值趋势；没有就不给（卡片落 note，不编趋势）。 */
  trend?: { text: string; dir: 'up' | 'down' | 'flat' }
}

defineProps<{ items: KpiItem[] }>()

/** 四颗线性图标（24 viewBox，stroke 取 currentColor）：节点网 / 预警铃 / 任务层 / 达标靶。
 *  顺序与 DashboardView 的 kpis 业务顺序一致，不按 label 字符串认。 */
const ICON_PATHS = [
  'M12 3v4m0 10v4M3 12h4m10 0h4M12 7a5 5 0 1 0 0 10 5 5 0 0 0 0-10zm-7.5-2.5L7 7m10 10 2.5 2.5m0-15L17 7M7 17l-2.5 2.5',
  'M12 3a6 6 0 0 0-6 6v3.5L4 16h16l-2-3.5V9a6 6 0 0 0-6-6zm-2 16a2 2 0 0 0 4 0M12 3v1',
  'M12 3 3 7.5l9 4.5 9-4.5L12 3zM3 12l9 4.5 9-4.5M3 16.5 12 21l9-4.5',
  'M12 3a9 9 0 1 0 9 9M12 7a5 5 0 1 0 5 5m-5-1 8.5-8.5M21 3l-1.5 4.5L15 9',
]

const TREND_ARROWS: Record<'up' | 'down' | 'flat', string> = {
  up: 'M12 19V5m0 0-6 6m6-6 6 6',
  down: 'M12 5v14m0 0 6-6m-6 6-6-6',
  flat: 'M4 12h16',
}
</script>

<template>
  <div class="kpi-strip">
    <!-- 页头（标题/说明/更新时间/刷新）由调用方塞进来：它和四张卡同属"第一屏的那一块" -->
    <div v-if="$slots.header" class="kpi-strip__header">
      <slot name="header" />
    </div>
    <div class="kpi-strip__cards">
      <div
        v-for="(item, index) in items"
        :key="item.label"
        class="kpi"
        :class="[`is-${item.tone ?? 'idle'}`, { 'is-primary': index === items.length - 1 }]"
      >
        <span class="kpi__icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
            <path :d="ICON_PATHS[index % ICON_PATHS.length]" />
          </svg>
        </span>
        <div class="kpi__main">
          <!-- 设计稿：数值在上、指标名下移；这一层只放数字 + 单位，
               测试靠它精确读出 "—个" / "137 条" -->
          <div class="kpi__row" :data-testid="`stat-${item.label}`">
            <span class="kpi__value" :class="{ 'is-empty': item.value === '—' }">{{ item.value }}</span>
            <span v-if="item.suffix" class="kpi__suffix">{{ item.suffix }}</span>
          </div>
          <div class="kpi__label">
            <span v-if="item.tone && item.tone !== 'idle'" class="kpi__dot" aria-hidden="true"></span>
            {{ item.label }}
          </div>
          <div v-if="item.trend" class="kpi__trend" :class="`is-${item.trend.dir}`">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path :d="TREND_ARROWS[item.trend.dir]" />
            </svg>
            {{ item.trend.text }}
          </div>
          <div v-else-if="item.note" class="kpi__note">{{ item.note }}</div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 托盘本身不再是一张卡：页头一行 + 四张独立数据卡，卡才是面板语言的承载者。 */
.kpi-strip {
  margin-bottom: var(--aeg-space-s4);
  container-type: inline-size;
}
.kpi-strip__header {
  margin-bottom: var(--aeg-space-s3);
}
.kpi-strip__cards {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: var(--aeg-space-s3);
}

.kpi {
  display: flex;
  align-items: center;
  gap: var(--aeg-space-s3);
  min-width: 0;
  padding: var(--aeg-space-s3) var(--aeg-space-s4);
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-lg);
  background: var(--aeg-color-card);
  box-shadow: var(--aeg-shadow-sm);
  transition:
    border-color var(--aeg-motion-base) ease,
    box-shadow var(--aeg-motion-base) ease,
    transform var(--aeg-motion-base) ease;
}
.kpi:hover {
  border-color: var(--aeg-color-brand-line);
  box-shadow: var(--aeg-shadow-md);
  transform: translateY(-1px);
}
/* 主指标（业务顺序最后一张）：品牌描边 + 图标底提一档，不靠紫色抢戏 */
.kpi.is-primary {
  border-color: var(--aeg-color-brand-line);
}
.kpi.is-primary .kpi__icon {
  background: var(--aeg-color-brand-soft);
  color: var(--aeg-color-brand);
}

.kpi__icon {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: center;
  width: 44px;
  height: 44px;
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-md);
  background: var(--aeg-color-sunken);
  color: var(--aeg-color-brand);
}
.kpi__icon svg {
  width: 24px;
  height: 24px;
}

.kpi__main {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.kpi__label {
  display: inline-flex;
  align-items: center;
  gap: var(--aeg-space-s1);
  font-size: var(--aeg-font-sm);
  font-weight: var(--aeg-weight-semibold);
  letter-spacing: 0.06em;
  color: var(--aeg-color-text-secondary);
  white-space: nowrap;
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

.kpi__row {
  display: flex;
  align-items: baseline;
  min-width: 0;
}
.kpi__value {
  font-family: var(--aeg-font-mono);
  font-variant-numeric: tabular-nums;
  font-size: var(--aeg-font-xl);
  font-weight: var(--aeg-weight-bold);
  line-height: var(--aeg-lh-tight);
  color: var(--aeg-color-text);
}
/* 没取到就是没取到：灰掉，不拿装饰把它扮成"一个状态" */
.kpi__value.is-empty {
  color: var(--aeg-color-text-muted);
}
.kpi__suffix {
  margin-left: var(--aeg-space-s1);
  font-size: var(--aeg-font-sm);
  font-weight: var(--aeg-weight-semibold);
  color: var(--aeg-color-text-muted);
}

.kpi__trend {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: var(--aeg-font-sm);
  font-variant-numeric: tabular-nums;
  color: var(--aeg-color-text-muted);
  white-space: nowrap;
}
.kpi__trend svg {
  width: 12px;
  height: 12px;
}
.kpi__trend.is-up {
  color: var(--aeg-color-status-success);
}
.kpi__trend.is-down {
  color: var(--aeg-color-status-danger);
}
.kpi__note {
  font-size: var(--aeg-font-sm);
  color: var(--aeg-color-text-muted);
  line-height: var(--aeg-lh-snug);
}
.kpi.is-ok .kpi__note {
  color: var(--aeg-color-status-success);
}

/* 卡片被挤窄时（助手抽屉拖宽）先降档成两列/一列，再收图标与内边距；
   断点按托盘内容宽度算，不跟视口走——抽屉挤压不会触发 @media。 */
@container (max-width: 900px) {
  .kpi-strip__cards {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
@container (max-width: 520px) {
  .kpi-strip__cards {
    grid-template-columns: minmax(0, 1fr);
  }
  .kpi {
    padding: var(--aeg-space-s2) var(--aeg-space-s3);
  }
  .kpi__icon {
    width: 36px;
    height: 36px;
  }
  .kpi__icon svg {
    width: 20px;
    height: 20px;
  }
}
</style>