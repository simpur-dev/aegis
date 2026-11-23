<script setup lang="ts">
/**
 * 覆盖式右栏（对齐 Dify `absolute top-14 right-0 bottom-1 z-10`）：
 * 400px 宽、从头部下沿起盖住画布，不再挤压画布宽度。
 *
 * 三档内容沿用 AEGIS 的“运行态 / 定义 / 节点”；关闭后焦点归还给打开它的按钮。
 */
import { nextTick, onBeforeUnmount, ref, useId } from 'vue'

const props = defineProps<{
  panel: 'run' | 'def' | 'node'
  waiting: number
  hasSelection: boolean
}>()

const emit = defineEmits<{ close: []; select: [panel: 'run' | 'def' | 'node'] }>()

const returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
const bodyRef = ref<HTMLElement | null>(null)
const tabs = ['run', 'def', 'node'] as const
const tabList = ref<HTMLElement | null>(null)
const panelId = useId()

onBeforeUnmount(() => {
  returnFocus?.focus()
})

function select(panel: 'run' | 'def' | 'node'): void {
  emit('select', panel)
  bodyRef.value?.scrollTo({ top: 0 })
}
function navigateTabs(event: KeyboardEvent): void {
  const current = tabs.indexOf(props.panel)
  const positions: Record<string, number> = {
    ArrowRight: (current + 1) % tabs.length,
    ArrowLeft: (current + tabs.length - 1) % tabs.length,
    Home: 0,
    End: tabs.length - 1,
  }
  const position = positions[event.key]
  if (position === undefined) return
  event.preventDefault()
  event.stopPropagation()
  select(tabs[position])
  void nextTick(() => tabList.value?.querySelector<HTMLElement>('[aria-selected="true"]')?.focus())
}
</script>

<template>
  <aside class="ed-panel" data-testid="editor-panel" aria-label="编排面板" @keydown.esc.stop="emit('close')">
    <header class="ed-panel__head">
      <div ref="tabList" class="ed-panel__tabs" role="tablist" aria-label="编排面板" @keydown="navigateTabs">
        <button
          type="button"
          role="tab"
          class="ed-panel__tab"
          :class="{ 'is-active': panel === 'run' }"
          :aria-selected="panel === 'run'"
          :tabindex="panel === 'run' ? 0 : -1"
          :id="`${panelId}-run`"
          :aria-controls="panelId"
          data-testid="panel-run"
          @click="select('run')"
        >
          运行态
          <span v-if="waiting > 0" class="ed-panel__count">{{ waiting }}</span>
        </button>
        <button
          type="button"
          role="tab"
          class="ed-panel__tab"
          :class="{ 'is-active': panel === 'def' }"
          :aria-selected="panel === 'def'"
          :tabindex="panel === 'def' ? 0 : -1"
          :id="`${panelId}-def`"
          :aria-controls="panelId"
          data-testid="panel-def"
          @click="select('def')"
        >
          定义
        </button>
        <button
          type="button"
          role="tab"
          class="ed-panel__tab"
          :class="{ 'is-active': panel === 'node' }"
          :aria-selected="panel === 'node'"
          :tabindex="panel === 'node' ? 0 : -1"
          :id="`${panelId}-node`"
          :aria-controls="panelId"
          data-testid="panel-node"
          @click="select('node')"
        >
          节点
        </button>
      </div>
      <button
        type="button"
        class="ed-panel__close"
        aria-label="关闭面板"
        data-testid="panel-close"
        @click="emit('close')"
      >
        ×
      </button>
    </header>
    <div ref="bodyRef" :id="panelId" class="ed-panel__body" role="tabpanel" :aria-labelledby="`${panelId}-${panel}`">
      <p v-if="panel === 'node' && !hasSelection" class="ed-panel__empty">
        先在画布上点一个节点，这里会显示它的参数与运行操作。
      </p>
      <slot v-else />
    </div>
  </aside>
</template>

<style scoped>
.ed-panel {
  position: absolute;
  top: 0;
  right: 0;
  bottom: 0;
  z-index: 10;
  display: flex;
  flex-direction: column;
  width: min(var(--aeg-editor-panel-width, 400px), 100%);
  border-left: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-xl) 0 0 var(--aeg-radius-xl);
  overflow: hidden;
  background: var(--aeg-color-overview-tile);
  box-shadow: var(--aeg-shadow-lg);
  color: var(--aeg-color-text);
}
.ed-panel__head {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: var(--aeg-space-s2);
  border-bottom: 1px solid var(--aeg-color-border-subtle);
}
.ed-panel__tabs {
  display: flex;
  gap: var(--aeg-space-s1);
  padding: var(--aeg-space-s1);
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-md);
  background: var(--aeg-color-sunken);
}
.ed-panel__tab {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 32px;
  padding: 0 10px;
  border: none;
  border-radius: var(--aeg-radius-sm);
  background: transparent;
  color: var(--aeg-color-text-muted);
  font: inherit;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
}
.ed-panel__tab:hover {
  color: var(--aeg-color-text);
}
.ed-panel__tab.is-active {
  background: var(--aeg-color-masthead-tab);
  color: var(--aeg-color-masthead-tab-ink);
}
.ed-panel__count {
  padding: 0 5px;
  border-radius: 999px;
  background: var(--aeg-color-brand-soft);
  color: var(--aeg-color-brand-hover);
  font-family: var(--aeg-font-mono);
  font-size: 10px;
  font-weight: 700;
}
.ed-panel__tab.is-active .ed-panel__count {
  color: inherit;
}
.ed-panel__close {
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
  border: none;
  border-radius: 7px;
  background: transparent;
  color: var(--aeg-color-text-muted);
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
}
.ed-panel__close:hover {
  background: var(--aeg-color-sunken);
  color: var(--aeg-color-text);
}
.ed-panel__body {
  flex: 1;
  min-height: 0;
  padding: 8px;
  overflow-y: auto;
  overscroll-behavior: contain;
}
.ed-panel__empty {
  margin: 0;
  padding: 24px 12px;
  color: var(--aeg-color-text-muted);
  font-size: 12px;
  line-height: 1.7;
}
@media (max-width: 720px) {
  .ed-panel {
    width: 100%;
    border-radius: 0;
  }
}
</style>
