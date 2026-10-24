<script setup lang="ts">
/**
 * 覆盖式右栏（对齐 Dify `absolute top-14 right-0 bottom-1 z-10`）：
 * 400px 宽、从头部下沿起盖住画布，不再挤压画布宽度。
 *
 * 三档内容沿用 AEGIS 的“运行态 / 定义 / 节点”；关闭后焦点归还给打开它的按钮。
 */
import { onBeforeUnmount, ref } from 'vue'

defineProps<{
  panel: 'run' | 'def' | 'node'
  waiting: number
  hasSelection: boolean
}>()

const emit = defineEmits<{ close: []; select: [panel: 'run' | 'def' | 'node'] }>()

const returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
const bodyRef = ref<HTMLElement | null>(null)

onBeforeUnmount(() => {
  returnFocus?.focus()
})

function select(panel: 'run' | 'def' | 'node'): void {
  emit('select', panel)
  bodyRef.value?.scrollTo({ top: 0 })
}
</script>

<template>
  <aside class="ed-panel" data-testid="editor-panel" aria-label="编排面板">
    <header class="ed-panel__head">
      <div class="ed-panel__tabs" role="tablist" aria-label="编排面板">
        <button
          type="button"
          role="tab"
          class="ed-panel__tab"
          :class="{ 'is-active': panel === 'run' }"
          :aria-selected="panel === 'run'"
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
    <div ref="bodyRef" class="ed-panel__body">
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
  background: var(--aeg-color-editor-panel);
  box-shadow: var(--aeg-shadow-lg);
  color: var(--aeg-color-text);
}
.ed-panel__head {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 8px 0;
  border-bottom: 1px solid var(--aeg-color-border-subtle);
}
.ed-panel__tabs {
  display: flex;
  gap: 2px;
}
.ed-panel__tab {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 32px;
  padding: 0 10px;
  border: none;
  border-bottom: 2px solid transparent;
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
  border-bottom-color: var(--aeg-color-brand);
  color: var(--aeg-color-brand);
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
  }
}
</style>
