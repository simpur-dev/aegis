<script setup lang="ts">
/**
 * 块选择器（移植 Dify `block-selector` 的结构与设计值，适配成 Vue + AEGIS 节点目录）。
 *
 * 400px 浮层：搜索常驻焦点、分类页签、最近使用、键盘方向/Enter/Escape、关闭后焦点归还。
 * 与 Dify 的差异：节点分类用 AEGIS 的四类（数据与执行 / 逻辑编排 / 智能分析 / 人工介入），
 * Start 已存在或准备插进边中间时禁用 Start（后端 Start 唯一、链路中间不能另起根）。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'

import { NODE_SPECS, PANEL_NODE_TYPES, type NodeCategory, type NodeType } from '@/utils/graph'

import NodeIcon from '../NodeIcon.vue'
import { NODE_META } from '../registry'
import { useEditorContext } from './context'
import type { InsertionRequest } from './insertion'

const props = defineProps<{ request: InsertionRequest }>()
const emit = defineEmits<{ select: [type: NodeType]; close: [] }>()

const { document: documentStore } = useEditorContext()

const RECENT_KEY = 'aegis.workflow.recent-blocks'
const RECENT_LIMIT = 4

const search = ref('')
const searchRef = ref<HTMLInputElement | null>(null)
const rootRef = ref<HTMLElement | null>(null)
const activeIndex = ref(0)
const recent = ref<NodeType[]>(readRecent())

const CATEGORY_LABELS: Record<NodeCategory, string> = {
  io: '数据与执行',
  logic: '逻辑编排',
  intelligence: '智能分析',
  human: '人工介入',
}
type TabKey = 'all' | NodeCategory
const TABS: Array<{ key: TabKey; label: string }> = [
  { key: 'all', label: '全部' },
  { key: 'io', label: '数据与执行' },
  { key: 'logic', label: '逻辑编排' },
  { key: 'intelligence', label: '智能分析' },
  { key: 'human', label: '人工介入' },
]
const tab = ref<TabKey>('all')

function isSelectable(type: string): type is NodeType {
  return (PANEL_NODE_TYPES as string[]).includes(type)
}

function readRecent(): NodeType[] {
  try {
    const raw = JSON.parse(localStorage.getItem(RECENT_KEY) ?? '[]') as unknown
    if (!Array.isArray(raw)) return []
    return raw.filter((item): item is NodeType => typeof item === 'string' && isSelectable(item)).slice(0, RECENT_LIMIT)
  } catch {
    return []
  }
}

function remember(type: NodeType): void {
  const next = [type, ...recent.value.filter((item) => item !== type)].slice(0, RECENT_LIMIT)
  recent.value = next
  try {
    localStorage.setItem(RECENT_KEY, JSON.stringify(next))
  } catch {
    /* 隐私模式存不下：本次会话仍按内存顺序显示 */
  }
}

const startExists = computed(() => documentStore.definition?.nodes.some((node) => node.type === 'start') ?? false)
const fromEdge = computed(() => props.request.edge !== undefined)

/** 禁用原因：空串即可选；同时驱动 aria-disabled 与行尾提示。 */
function disabledReason(type: NodeType): string {
  if (type === 'start' && fromEdge.value) return '不能插在链路中间'
  if (type === 'start' && startExists.value) return '已有开始节点'
  return ''
}

interface Item {
  type: NodeType
  label: string
  description: string
  category: NodeCategory
}

interface Group {
  key: string
  label: string
  items: Item[]
}

const CATEGORY_ORDER: NodeCategory[] = ['io', 'logic', 'intelligence', 'human']

const items: Item[] = PANEL_NODE_TYPES.map((type) => ({
  type,
  label: NODE_META[type].label,
  description: NODE_META[type].description,
  category: NODE_SPECS[type].category,
}))

const keyword = computed(() => search.value.trim().toLowerCase())

function matches(item: Item): boolean {
  if (keyword.value === '') return true
  return `${item.label} ${item.type} ${item.description}`.toLowerCase().includes(keyword.value)
}

const groups = computed<Group[]>(() => {
  const pool = items.filter((item) => (tab.value === 'all' || item.category === tab.value) && matches(item))
  const list: Group[] = []
  if (tab.value === 'all' && keyword.value === '') {
    const recentItems = recent.value
      .map((type) => pool.find((item) => item.type === type))
      .filter((item): item is Item => item !== undefined)
    if (recentItems.length > 0) list.push({ key: 'recent', label: '最近使用', items: recentItems })
  }
  const rest =
    tab.value === 'all' && keyword.value === '' ? pool.filter((item) => !recent.value.includes(item.type)) : pool
  for (const category of CATEGORY_ORDER) {
    const rows = rest.filter((item) => item.category === category)
    if (rows.length > 0) list.push({ key: category, label: CATEGORY_LABELS[category], items: rows })
  }
  return list
})

const flat = computed<Item[]>(() => groups.value.flatMap((group) => group.items))
/** 搜索过滤后 activeIndex 可能越界；渲染与键盘都按这个收敛值走。 */
const activeOption = computed(() => (flat.value.length === 0 ? -1 : Math.min(activeIndex.value, flat.value.length - 1)))

function choose(item: Item): void {
  if (disabledReason(item.type) !== '') return
  remember(item.type)
  emit('select', item.type)
}

function move(delta: number): void {
  if (flat.value.length === 0) return
  activeIndex.value = (activeOption.value + delta + flat.value.length) % flat.value.length
  const id = `block-option-${activeIndex.value}`
  void nextTick(() => document.getElementById(id)?.scrollIntoView({ block: 'nearest' }))
}

function onSearchKeydown(event: KeyboardEvent): void {
  if (event.key === 'ArrowDown') {
    event.preventDefault()
    move(1)
  } else if (event.key === 'ArrowUp') {
    event.preventDefault()
    move(-1)
  } else if (event.key === 'Enter') {
    event.preventDefault()
    const item = flat.value[activeOption.value]
    if (item !== undefined) choose(item)
  } else if (event.key === 'Escape') {
    event.preventDefault()
    emit('close')
  }
}

function onTabKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') {
    event.preventDefault()
    emit('close')
  }
}

function onDocumentPointerDown(event: MouseEvent): void {
  if (!(event.target instanceof Node)) return
  if (rootRef.value?.contains(event.target) === true) return
  emit('close')
}

const returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null

onMounted(() => {
  void nextTick(() => searchRef.value?.focus())
  document.addEventListener('mousedown', onDocumentPointerDown)
})
onBeforeUnmount(() => {
  document.removeEventListener('mousedown', onDocumentPointerDown)
  returnFocus?.focus()
})

const POPUP_WIDTH = 400
const POPUP_HEIGHT = 480
const MARGIN = 8

const popupStyle = computed(() => {
  const width = Math.min(POPUP_WIDTH, window.innerWidth - MARGIN * 2)
  const left = Math.min(Math.max(props.request.x, MARGIN), window.innerWidth - width - MARGIN)
  const openDown = props.request.y + MARGIN + POPUP_HEIGHT <= window.innerHeight - MARGIN
  const top = Math.min(
    Math.max(openDown ? props.request.y + MARGIN : props.request.y - POPUP_HEIGHT - MARGIN, MARGIN),
    Math.max(MARGIN, window.innerHeight - MARGIN - POPUP_HEIGHT),
  )
  return { left: `${left}px`, top: `${top}px`, width: `${width}px` }
})
</script>

<template>
  <div
    ref="rootRef"
    class="block-selector"
    :style="popupStyle"
    role="dialog"
    aria-modal="false"
    aria-label="添加节点"
    data-testid="block-selector"
  >
    <div class="block-selector__search">
      <input
        ref="searchRef"
        v-model="search"
        class="block-selector__input"
        type="search"
        placeholder="搜索节点"
        aria-label="搜索节点"
        role="combobox"
        aria-expanded="true"
        aria-controls="block-options"
        @keydown="onSearchKeydown"
        @input="activeIndex = 0"
      />
    </div>
    <div class="block-selector__tabs" role="tablist" aria-label="节点分类" @keydown="onTabKeydown">
      <button
        v-for="entry in TABS"
        :key="entry.key"
        type="button"
        role="tab"
        class="block-selector__tab"
        :class="{ 'is-active': tab === entry.key }"
        :aria-selected="tab === entry.key"
        @click="((tab = entry.key), (activeIndex = 0))"
      >
        {{ entry.label }}
      </button>
    </div>
    <div id="block-options" class="block-selector__list" role="listbox" aria-label="可添加节点">
      <p v-if="flat.length === 0" class="block-selector__empty">没有匹配的节点</p>
      <template v-else>
        <section v-for="group in groups" :key="group.key" class="block-selector__group">
          <h4 class="block-selector__group-title">{{ group.label }}</h4>
          <button
            v-for="item in group.items"
            :id="`block-option-${flat.indexOf(item)}`"
            :key="item.type"
            type="button"
            class="block-selector__row"
            :class="{
              'is-disabled': disabledReason(item.type) !== '',
              'is-active': flat.indexOf(item) === activeOption,
            }"
            role="option"
            :aria-selected="flat.indexOf(item) === activeOption"
            :aria-disabled="disabledReason(item.type) !== '' || undefined"
            :title="item.description"
            @click="choose(item)"
            @mouseenter="activeIndex = flat.indexOf(item)"
          >
            <span class="block-selector__icon" :class="`is-${item.category}`">
              <NodeIcon :type="item.type" />
            </span>
            <span class="block-selector__label">{{ item.label }}</span>
            <span v-if="disabledReason(item.type) !== ''" class="block-selector__hint">
              {{ disabledReason(item.type) }}
            </span>
            <span v-else class="block-selector__type">{{ item.type }}</span>
          </button>
        </section>
      </template>
    </div>
  </div>
</template>

<style scoped>
.block-selector {
  position: fixed;
  z-index: 40;
  display: flex;
  flex-direction: column;
  max-height: 480px;
  overflow: hidden;
  border: 1px solid var(--aeg-color-border);
  border-radius: 12px;
  background: var(--aeg-color-editor-panel);
  box-shadow: var(--aeg-shadow-lg);
  color: var(--aeg-color-text);
}
.block-selector__search {
  position: relative;
  margin: 8px;
}
.block-selector__input {
  width: 100%;
  height: 32px;
  padding: 0 12px;
  border: 1px solid var(--aeg-color-border);
  border-radius: 8px;
  background: var(--aeg-color-sunken);
  color: var(--aeg-color-text);
  font: inherit;
  font-size: 13px;
  outline: none;
}
.block-selector__input:focus {
  border-color: var(--aeg-color-brand);
  box-shadow: 0 0 0 2px var(--aeg-color-brand-line);
}
.block-selector__tabs {
  display: flex;
  flex: 0 0 auto;
  gap: 0;
  padding: 0 4px;
  overflow-x: auto;
  border-bottom: 1px solid var(--aeg-color-border-subtle);
}
.block-selector__tab {
  height: 32px;
  padding: 0 10px;
  border: none;
  border-radius: 8px 8px 0 0;
  background: transparent;
  color: var(--aeg-color-text-muted);
  font: inherit;
  font-size: 12px;
  font-weight: 500;
  white-space: nowrap;
  cursor: pointer;
}
.block-selector__tab:hover {
  color: var(--aeg-color-text);
}
.block-selector__tab.is-active {
  background: var(--aeg-color-editor-panel);
  color: var(--aeg-color-brand);
}
.block-selector__list {
  flex: 1;
  min-height: 0;
  padding: 4px;
  overflow-y: auto;
}
.block-selector__group-title {
  margin: 0;
  padding: 6px 12px 2px;
  color: var(--aeg-color-text-muted);
  font-size: 11px;
  font-weight: 500;
}
.block-selector__row {
  display: flex;
  align-items: center;
  width: 100%;
  height: 32px;
  padding: 0 12px;
  border: none;
  border-radius: 8px;
  background: transparent;
  color: var(--aeg-color-text-secondary);
  font: inherit;
  font-size: 13px;
  text-align: left;
  cursor: pointer;
}
.block-selector__row:hover,
.block-selector__row.is-active {
  background: var(--aeg-color-sunken);
  color: var(--aeg-color-text);
}
.block-selector__row.is-disabled {
  cursor: not-allowed;
  opacity: 0.55;
}
.block-selector__icon {
  display: grid;
  flex: 0 0 auto;
  place-items: center;
  width: 24px;
  height: 24px;
  margin-right: 8px;
  border-radius: 6px;
  background: var(--aeg-color-sunken);
  color: var(--aeg-color-text-secondary);
}
.block-selector__icon.is-io {
  color: var(--aeg-color-editor-cat-io);
}
.block-selector__icon.is-logic {
  color: var(--aeg-color-editor-cat-logic);
}
.block-selector__icon.is-intelligence {
  color: var(--aeg-color-editor-cat-intelligence);
}
.block-selector__icon.is-human {
  color: var(--aeg-color-editor-cat-human);
}
.block-selector__label {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.block-selector__type,
.block-selector__hint {
  flex: 0 0 auto;
  margin-left: 8px;
  color: var(--aeg-color-text-muted);
  font-family: var(--aeg-font-mono);
  font-size: 11px;
}
.block-selector__hint {
  font-family: var(--aeg-font-sans);
}
.block-selector__empty {
  margin: 0;
  padding: 16px 12px;
  color: var(--aeg-color-text-muted);
  font-size: 12px;
}
</style>
