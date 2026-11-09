<script setup lang="ts">
/**
 * 风险瓦片墙（spike：dashboard 标杆页）。
 *
 * 与旧"风险区域网格"的差别只有一处口径：**排序即层级**。
 * 旧版按 Map 插入序摆均一格，三条红色结论并排时读不出"先看哪个"；
 * 这里等级升序（1 最坏在前）、同级按置信降序，值班员从左到右扫就是严重度从高到低。
 *
 * 诚实性规矩原样继承：
 * ① 等级→颜色仍取 `api/types.ts` 的 `RISK_COLORS`（全站同一份，图例与图上不许两张皮）；
 * ② 没研判出风险的链路只能说"未标注区域"，不许从任务单元号上截一段冒充区划代码；
 * ③ 置信没量到写 `—`，不写 0%。
 */
import { computed } from 'vue'

import type { RiskLevel } from '@/api/types'
import { RISK_COLORS, RISK_LABELS } from '@/api/types'

export interface RiskCell {
  region: string
  hazard: string
  level: RiskLevel
  confidence: number
  degraded: boolean
  isMock: boolean
  disclaimer: string
}

const props = defineProps<{ cells: RiskCell[]; windowSize: number }>()

/**
 * 墙上有 mock 结论就得挂一句使用限制；文案优先用结论自带的 `disclaimer`
 * （外部实现的"禁止用于实际预警决策"就写在那里），没有就给一句统一的。
 */
const mockNotice = computed<string>(() => {
  const mocked = props.cells.filter((cell) => cell.isMock)
  if (!mocked.length) return ''
  return mocked.find((cell) => cell.disclaimer)?.disclaimer ?? '含模拟数据结论，仅供演练与契约联调，不得作为实际预警依据。'
})
</script>

<template>
  <div class="risk-wall">
    <div v-if="mockNotice" class="risk-wall__notice" data-testid="risk-mock-notice" role="note">
      <span class="risk-wall__notice-tag">模拟数据</span>
      <span>{{ mockNotice }}</span>
    </div>
    <div
      v-for="cell in cells"
      :key="cell.region"
      class="cell"
      :data-testid="`risk-cell-${cell.region}`"
      :data-level="cell.level"
    >
      <span class="cell__band" :style="{ background: RISK_COLORS[cell.level] }" aria-hidden="true"></span>
      <div class="cell__body">
        <div class="cell__head">
          <span class="region">{{ cell.region }}</span>
          <span class="cell__level" :style="{ color: `var(--aeg-risk-text-${cell.level})` }">{{ RISK_LABELS[cell.level] }}</span>
        </div>
        <div class="cell__hazard">{{ cell.hazard }}</div>
        <div class="cell__facts">
          <span class="cell__conf">置信 {{ cell.confidence > 0 ? `${Math.round(cell.confidence * 100)}%` : '—' }}</span>
          <span v-if="cell.isMock" class="cell__mock" :title="cell.disclaimer || '该结论来自模拟数据'">模拟数据</span>
          <span v-if="cell.degraded" class="cell__degraded">降级：平台规则兜底</span>
        </div>
      </div>
    </div>
    <!-- 卡片级空态一律用带出口的芯片块：同一屏不许并排两种空态习语（antd 居中大插图没有出口）。 -->
    <div v-if="!cells.length" class="empty-hero empty-hero--compact" data-testid="region-empty">
      <span class="empty-hero__icon" aria-hidden="true">◇</span>
      <div>
        <div class="empty-hero__title">还没有研判结论</div>
        <div class="empty-hero__desc">这面墙按最近 {{ windowSize }} 条链路归集，跑一次演练就会落点。</div>
        <router-link class="empty-hero__action empty-hero__cta" to="/monitor">去监测页发起演练 →</router-link>
      </div>
    </div>
  </div>
</template>

<style scoped>
.risk-wall {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
  gap: var(--aeg-space-s2);
}
/* 免责声明横跨整面墙：它约束的是墙上全部瓦片，不是某一格 */
.risk-wall__notice {
  display: flex;
  grid-column: 1 / -1;
  flex-wrap: wrap;
  gap: var(--aeg-space-s2);
  align-items: baseline;
  padding: var(--aeg-space-s1) var(--aeg-space-s3);
  border: 1px dashed var(--aeg-color-status-warning);
  border-radius: var(--aeg-radius-md);
  color: var(--aeg-color-text-secondary);
  font-size: var(--aeg-font-xs);
}
.risk-wall__notice-tag {
  flex: 0 0 auto;
  font-weight: var(--aeg-weight-semibold);
  color: var(--aeg-color-status-warning);
}
/* 空态要占满整面墙，不是挤进一个 190px 的格子（"短词折行"判据抓过的） */
.risk-wall > .empty-hero {
  grid-column: 1 / -1;
}
.cell {
  display: flex;
  overflow: hidden;
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-md);
  background: var(--aeg-color-card);
}
.cell__band {
  flex: 0 0 4px;
}
.cell__body {
  min-width: 0;
  padding: var(--aeg-space-s2) var(--aeg-space-s3);
}
.cell__head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--aeg-space-s2);
}
.region {
  font-family: var(--aeg-font-mono);
  font-size: var(--aeg-font-base);
  font-weight: var(--aeg-weight-semibold);
  color: var(--aeg-color-text);
}
.cell__level {
  flex: 0 0 auto;
  font-size: var(--aeg-font-sm);
  font-weight: var(--aeg-weight-bold);
}
.cell__hazard {
  margin-top: 2px;
  font-size: var(--aeg-font-sm);
  color: var(--aeg-color-text-secondary);
}
.cell__facts {
  display: flex;
  flex-wrap: wrap;
  gap: var(--aeg-space-s1) var(--aeg-space-s2);
  margin-top: var(--aeg-space-s1);
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-text-muted);
}
.cell__degraded {
  color: var(--aeg-color-status-warning);
}
.cell__mock {
  padding: 0 4px;
  border: 1px solid var(--aeg-color-status-warning);
  border-radius: 3px;
  color: var(--aeg-color-status-warning);
  font-weight: var(--aeg-weight-semibold);
}
</style>
