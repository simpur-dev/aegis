<script setup lang="ts">
/**
 * 装配事实面板：每条可选腿的健康三态（未启用 / 运行中 / 降级运行）。
 *
 * 为什么值得占指挥台一页：后端的降级设计（`backend/src/aegis/integrations/`）只有接口能
 * 看见时，现场判断"预案为什么变慢"仍然要靠人翻日志。这里把 `/api/v1/integrations` 摊开，
 * 并区分"读不到"（接口失败，必须显眼）与"读到了、都没问题"——把这两件事混成一个空状态，
 * 等于在一次真故障上显示"一切正常"。
 *
 * 行是后端给的，这里不硬编码腿清单：后端加一条腿，面板上就多一行，不存在"忘了跟进"这种藏法。
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'

import type { IntegrationRow, IntegrationSnapshot } from '@/api/integrations'
import { fetchIntegrations, legLabel, legState, legStateLabel, visibleDetail } from '@/api/integrations'

const props = withDefaults(defineProps<{ refreshMs?: number }>(), { refreshMs: 15_000 })

const snapshot = ref<IntegrationSnapshot | null>(null)
const error = ref<string | null>(null)
const loading = ref(false)
let timer: ReturnType<typeof setInterval> | null = null

/** 三态各自的视觉强度：未启用是中性信息，降级是必须被看见的异常。 */
const TAG_COLORS: Record<ReturnType<typeof legState>, string> = { disabled: 'default', enabled: 'green', degraded: 'orange' }

async function refresh(): Promise<void> {
  loading.value = true
  try {
    snapshot.value = await fetchIntegrations()
    error.value = null
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : String(reason)
  } finally {
    loading.value = false
  }
}

function rows(): IntegrationRow[] {
  return snapshot.value?.items ?? []
}

onMounted(() => {
  void refresh()
  // 页签切到后台就不发请求：大屏的副页签没人看，15s 一次的空轮询却一直在打接口。
  timer = setInterval(() => {
    if (document.visibilityState !== 'visible') return
    void refresh()
  }, props.refreshMs)
})

onBeforeUnmount(() => {
  if (timer !== null) clearInterval(timer)
})
</script>

<template>
  <a-card size="small" title="可选子系统：接得上、退得掉、看得见" :loading="loading && rows().length === 0">
    <template #extra>
      <a-space size="small">
        <a-tag v-if="error" color="red" data-testid="panel-state">读不到装配事实</a-tag>
        <a-tag v-else-if="snapshot?.all_enabled" color="green" data-testid="panel-state">全部启用</a-tag>
        <a-tag v-else color="orange" data-testid="panel-state">有腿未启用</a-tag>
        <a-button size="small" :loading="loading" @click="refresh">刷新</a-button>
      </a-space>
    </template>

    <a-alert v-if="error" type="error" show-icon :message="error" data-testid="panel-error" class="leg-error" />

    <div class="leg-grid">
      <div v-for="row in rows()" :key="row.name" class="leg-row" :data-testid="`leg-${row.name}`">
        <a-tag :color="TAG_COLORS[legState(row)]" :data-testid="`state-${row.name}`">{{ legStateLabel(legState(row)) }}</a-tag>
        <div class="leg-main">
          <div class="leg-name">{{ legLabel(row.name) }}</div>
          <div class="leg-detail">driver={{ row.driver || '—' }}</div>
          <div class="leg-facts">
            <span
              v-for="entry in visibleDetail(row)"
              :key="entry.key"
              :title="entry.hint"
              :data-testid="`detail-${row.name}-${entry.key}`"
            >
              {{ entry.key }}={{ entry.value }}
            </span>
          </div>
        </div>
      </div>
    </div>

    <div v-if="!rows().length && !loading" class="leg-empty" data-testid="legs-empty">
      {{ error ? '装配事实不可读' : '后端未上报任何可选腿' }}
    </div>
  </a-card>
</template>

<style scoped>
.leg-error {
  margin-bottom: var(--aeg-space-s2);
}
.leg-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 0 var(--aeg-space-s4);
}
.leg-row {
  display: flex;
  align-items: flex-start;
  gap: var(--aeg-space-s2);
  padding: 6px 0;
  /* 原先是 rgba(0,0,0,0.06)：浅色限定字面色，深色主题下等于没画（棘轮与换肤双料欠账）。 */
  border-bottom: 1px solid var(--aeg-color-border-subtle);
}
.leg-main {
  min-width: 0;
  /* 长事实串（如 dataset=内置预案模板…）原先一路顶出卡片、压到右邻那一行上：
     这里给它断行的余地，事实区自己换行。 */
  overflow-wrap: anywhere;
}
.leg-name {
  font-size: var(--aeg-font-base);
  font-weight: var(--aeg-weight-semibold);
  color: var(--aeg-color-text);
}
.leg-detail {
  font-family: var(--aeg-font-mono);
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-text-secondary);
}
.leg-facts {
  display: flex;
  flex-wrap: wrap;
  gap: 2px var(--aeg-space-s2);
  overflow-wrap: anywhere;
}
.leg-facts span {
  font-family: var(--aeg-font-mono);
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-text-muted);
}
.leg-empty {
  padding: var(--aeg-space-s2) 0;
  font-size: var(--aeg-font-sm);
  color: var(--aeg-color-text-secondary);
}
</style>
