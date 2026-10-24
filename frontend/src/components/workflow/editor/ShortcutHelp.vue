<script setup lang="ts">
/**
 * `?` 打开的快捷键一览：分组与顺序来自 `shortcuts/definitions.ts`，
 * 展示键取 displayHotkey（与上游帮助面板同口径）。
 */
import { onBeforeUnmount, onMounted } from 'vue'

import {
  getWorkflowCanvasShortcutDisplayKey,
  WORKFLOW_CANVAS_SHORTCUT_GROUPS,
  WORKFLOW_CANVAS_SHORTCUTS,
} from '@/components/workflow/shortcuts/definitions'

const emit = defineEmits<{ close: [] }>()

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') emit('close')
}

onMounted(() => window.addEventListener('keydown', onKeydown))
onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))
</script>

<template>
  <Teleport to="body">
    <div class="ed-help-mask" data-testid="shortcut-help" @click.self="emit('close')">
      <section class="ed-help" role="dialog" aria-modal="true" aria-label="快捷键">
        <header class="ed-help__head">
          <h2 class="ed-help__title">快捷键</h2>
          <button type="button" class="ed-help__close" aria-label="关闭" @click="emit('close')">×</button>
        </header>
        <div class="ed-help__groups">
          <section v-for="group in WORKFLOW_CANVAS_SHORTCUT_GROUPS" :key="group.title" class="ed-help__group">
            <h3 class="ed-help__group-title">{{ group.title }}</h3>
            <dl class="ed-help__list">
              <template v-for="id in group.ids" :key="id">
                <dt>{{ WORKFLOW_CANVAS_SHORTCUTS[id].name }}</dt>
                <dd>
                  <kbd>{{ getWorkflowCanvasShortcutDisplayKey(id) }}</kbd>
                </dd>
              </template>
            </dl>
          </section>
        </div>
        <footer class="ed-help__foot">按 Esc 或点击空白处关闭</footer>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.ed-help-mask {
  position: fixed;
  inset: 0;
  z-index: 70;
  display: grid;
  place-items: center;
  background: var(--aeg-color-overlay-scrim);
}
.ed-help {
  width: min(560px, calc(100vw - 48px));
  max-height: min(640px, calc(100vh - 80px));
  overflow: auto;
  padding: 18px 20px;
  border: 1px solid var(--aeg-color-border);
  border-radius: 14px;
  background: var(--aeg-color-editor-panel);
  box-shadow: var(--aeg-shadow-lg);
}
.ed-help__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}
.ed-help__title {
  margin: 0;
  font-size: 15px;
  font-weight: 700;
}
.ed-help__close {
  width: 28px;
  height: 28px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--aeg-color-text-secondary);
  font-size: 18px;
  cursor: pointer;
}
.ed-help__close:hover {
  background: var(--aeg-color-sunken);
  color: var(--aeg-color-text);
}
.ed-help__groups {
  display: grid;
  gap: 14px;
}
.ed-help__group-title {
  margin: 0 0 6px;
  color: var(--aeg-color-text-muted);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.08em;
}
.ed-help__list {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 6px 16px;
  margin: 0;
}
.ed-help__list dt {
  color: var(--aeg-color-text-secondary);
  font-size: 12px;
}
.ed-help__list dd {
  margin: 0;
  text-align: right;
}
.ed-help__list kbd {
  display: inline-block;
  min-width: 28px;
  padding: 1px 6px;
  border: 1px solid var(--aeg-color-border);
  border-radius: 5px;
  background: var(--aeg-color-sunken);
  color: var(--aeg-color-text);
  font: inherit;
  font-size: 11px;
  text-align: center;
}
.ed-help__foot {
  margin-top: 14px;
  color: var(--aeg-color-text-muted);
  font-size: 11px;
  text-align: center;
}
</style>
