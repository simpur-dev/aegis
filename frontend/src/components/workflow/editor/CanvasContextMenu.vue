<script setup lang="ts">
/** 画布/节点/连线的统一右键菜单（对齐 Dify 画布右键条目的分组与禁用态）。 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import type { ContextMenuItem } from './menus'

const props = defineProps<{ x: number; y: number; items: readonly ContextMenuItem[] }>()
const emit = defineEmits<{ select: [id: string]; close: [] }>()

const root = ref<HTMLElement | null>(null)
const MENU_WIDTH = 208
const ITEM_HEIGHT = 32

const style = computed(() => {
  const rows = Math.min(props.items.length, 12)
  const left = Math.max(8, Math.min(props.x, window.innerWidth - MENU_WIDTH - 8))
  const top = Math.max(8, Math.min(props.y, window.innerHeight - rows * ITEM_HEIGHT - 24))
  return { left: `${left}px`, top: `${top}px`, width: `${MENU_WIDTH}px` }
})

function choose(id: string): void {
  emit('select', id)
  emit('close')
}

function onPointerDown(event: PointerEvent): void {
  if (root.value !== null && event.target instanceof Node && root.value.contains(event.target)) return
  emit('close')
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') emit('close')
}

function onWindowBlur(): void {
  emit('close')
}

onMounted(() => {
  window.addEventListener('pointerdown', onPointerDown, true)
  window.addEventListener('keydown', onKeydown)
  window.addEventListener('blur', onWindowBlur)
})
onBeforeUnmount(() => {
  window.removeEventListener('pointerdown', onPointerDown, true)
  window.removeEventListener('keydown', onKeydown)
  window.removeEventListener('blur', onWindowBlur)
})
</script>

<template>
  <Teleport to="body">
    <div ref="root" class="ed-menu" role="menu" :style="style" data-testid="canvas-context-menu" @contextmenu.prevent>
      <template v-for="item in props.items" :key="item.id">
        <div v-if="item.separator" class="ed-menu__sep" role="separator" />
        <button
          v-else
          type="button"
          role="menuitem"
          class="ed-menu__item"
          :class="{ 'is-danger': item.danger }"
          :disabled="item.disabled === true"
          @click="choose(item.id)"
        >
          <span>{{ item.label }}</span>
          <kbd v-if="item.shortcut !== undefined" class="ed-menu__key">{{ item.shortcut }}</kbd>
        </button>
      </template>
    </div>
  </Teleport>
</template>

<style scoped>
.ed-menu {
  position: fixed;
  z-index: 60;
  padding: 5px;
  border: 1px solid var(--aeg-color-border);
  border-radius: 10px;
  background: var(--aeg-color-editor-panel);
  box-shadow: var(--aeg-shadow-md);
}
.ed-menu__item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  width: 100%;
  height: 30px;
  padding: 0 9px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--aeg-color-text);
  font: inherit;
  font-size: 12px;
  text-align: left;
  cursor: pointer;
}
.ed-menu__item:hover:not(:disabled) {
  background: var(--aeg-color-sunken);
}
.ed-menu__item:disabled {
  color: var(--aeg-color-text-muted);
  cursor: not-allowed;
}
.ed-menu__item.is-danger:not(:disabled) {
  color: var(--aeg-tag-red-text);
}
.ed-menu__item.is-danger:hover:not(:disabled) {
  background: var(--aeg-tag-red-bg);
}
.ed-menu__key {
  color: var(--aeg-color-text-muted);
  font: inherit;
  font-size: 11px;
}
.ed-menu__sep {
  height: 1px;
  margin: 4px 6px;
  background: var(--aeg-color-border);
}
</style>
