<script setup lang="ts">
/** `Mod+F` 的按名称定位：输入过滤 → ↑↓ 选择 → Enter 聚焦（对齐 Dify 的节点搜索面板）。 */
import { computed, nextTick, onMounted, ref } from 'vue'

import type { LocatorNode } from './menus'

const props = defineProps<{ nodes: readonly LocatorNode[] }>()
const emit = defineEmits<{ close: []; pick: [id: string] }>()

const query = ref('')
const activeIndex = ref(0)
const input = ref<HTMLInputElement | null>(null)

const matches = computed(() => {
  const needle = query.value.trim().toLowerCase()
  if (needle === '') return props.nodes.slice(0, 20)
  return props.nodes
    .filter((node) => node.name.toLowerCase().includes(needle) || node.id.toLowerCase().includes(needle))
    .slice(0, 20)
})

function move(delta: number): void {
  if (matches.value.length === 0) return
  activeIndex.value = (activeIndex.value + delta + matches.value.length) % matches.value.length
}

function pick(id: string): void {
  emit('pick', id)
  emit('close')
}

function onQuery(value: string): void {
  query.value = value
  activeIndex.value = 0
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') {
    event.preventDefault()
    emit('close')
    return
  }
  if (event.key === 'ArrowDown') {
    event.preventDefault()
    move(1)
    return
  }
  if (event.key === 'ArrowUp') {
    event.preventDefault()
    move(-1)
    return
  }
  if (event.key === 'Enter') {
    const target = matches.value[activeIndex.value] ?? matches.value[0]
    if (target !== undefined) {
      event.preventDefault()
      pick(target.id)
    }
  }
}

onMounted(() => {
  void nextTick(() => input.value?.focus())
})
</script>

<template>
  <Teleport to="body">
    <div class="ed-locator-mask" data-testid="node-locator" @click.self="emit('close')">
      <section class="ed-locator" role="dialog" aria-modal="true" aria-label="定位节点">
        <input
          ref="input"
          class="ed-locator__input"
          type="text"
          placeholder="按名称或 ID 查找节点"
          :value="query"
          @input="onQuery(($event.target as HTMLInputElement).value)"
          @keydown="onKeydown"
        />
        <ul class="ed-locator__list" role="listbox">
          <li v-if="matches.length === 0" class="ed-locator__empty">没有匹配的节点</li>
          <li v-for="(node, index) in matches" :key="node.id">
            <button
              type="button"
              role="option"
              class="ed-locator__item"
              :class="{ 'is-active': index === activeIndex }"
              :aria-selected="index === activeIndex"
              @mouseenter="activeIndex = index"
              @click="pick(node.id)"
            >
              <span class="ed-locator__name">{{ node.name }}</span>
              <span class="ed-locator__meta">{{ node.type }} · {{ node.id }}</span>
            </button>
          </li>
        </ul>
        <footer class="ed-locator__foot">↑↓ 选择 · Enter 定位 · Esc 关闭</footer>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.ed-locator-mask {
  position: fixed;
  inset: 0;
  z-index: 70;
  display: grid;
  align-items: start;
  justify-items: center;
  padding-top: 12vh;
  background: var(--aeg-color-overlay-scrim);
}
.ed-locator {
  width: min(460px, calc(100vw - 48px));
  overflow: hidden;
  border: 1px solid var(--aeg-color-border);
  border-radius: 14px;
  background: var(--aeg-color-editor-panel);
  box-shadow: var(--aeg-shadow-lg);
}
.ed-locator__input {
  width: 100%;
  height: 44px;
  padding: 0 16px;
  border: 0;
  border-bottom: 1px solid var(--aeg-color-border);
  background: transparent;
  color: var(--aeg-color-text);
  font: inherit;
  font-size: 13px;
  outline: none;
}
.ed-locator__list {
  max-height: 320px;
  margin: 0;
  padding: 6px;
  overflow: auto;
  list-style: none;
}
.ed-locator__item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  width: 100%;
  padding: 8px 10px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--aeg-color-text);
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.ed-locator__item.is-active {
  background: var(--aeg-color-brand-soft);
}
.ed-locator__name {
  font-size: 12px;
  font-weight: 600;
}
.ed-locator__meta {
  color: var(--aeg-color-text-muted);
  font-size: 11px;
}
.ed-locator__empty {
  padding: 14px 10px;
  color: var(--aeg-color-text-muted);
  font-size: 12px;
  text-align: center;
}
.ed-locator__foot {
  padding: 8px 12px;
  border-top: 1px solid var(--aeg-color-border);
  color: var(--aeg-color-text-muted);
  font-size: 11px;
  text-align: center;
}
</style>
