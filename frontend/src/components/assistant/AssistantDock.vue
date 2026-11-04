<script setup lang="ts">
/**
 * AssistantDock —— 全站悬浮的「智能助手」图标 + 右侧抽屉。
 *
 * - 图标只能上下拖：水平位置由 CSS 固定（收起贴右边缘，展开贴抽屉左侧）；
 * - 单击（位移 < 4px）开/关右侧抽屉，拖拽松手不会误触；
 * - 竖直位置与开关状态存 localStorage，刷新后还原。
 *
 * 纯计算（夹紧 / 点击判定）在 dock.ts，本组件只管 DOM 与事件。
 */

import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import AssistantPanel from '@/components/assistant/AssistantPanel.vue'
import { useAlerts } from '@/composables/useAlerts'
import {
  clampDrawerWidth,
  clampTop,
  defaultDrawerWidth,
  DOCK_SIZE,
  DRAWER_MAX,
  DRAWER_MIN,
  isClick,
} from '@/components/assistant/dock'

const props = defineProps<{ immersive?: boolean }>()

const STORAGE_KEY = 'aegis.assistant.dock'

const dockEl = ref<HTMLButtonElement | null>(null)
const open = ref(false)
const dragging = ref(false)
const resizing = ref(false)
const top = ref(0)
const width = ref(0)
const maxWidth = ref(DRAWER_MAX)
const headerH = ref(68)

const headerVar = computed(() => (props.immersive ? '--aeg-editor-header-height' : '--aeg-header-height'))

/** 抽屉顶部的落点（跟随顶栏/编辑器头部高度；水平位置与宽度由 CSS 变量算）。 */
const drawerTopStyle = computed(() => ({ top: `${headerH.value}px` }))

const { urgentCards } = useAlerts()
watch(
  () => urgentCards.value.length,
  (now, prev) => {
    // Ⅰ/Ⅱ 级告警出现（0 → N）：抽屉自动展开，不等值班员去找图标（需求 D3 的强打断）
    if (now > 0 && (prev ?? 0) === 0) {
      open.value = true
      saveState()
    }
  },
)

function readHeaderHeight(): number {
  const raw = getComputedStyle(document.documentElement).getPropertyValue(headerVar.value).trim()
  const px = Number.parseFloat(raw)
  return Number.isFinite(px) && px > 0 ? px : 68
}

function loadState(): { top?: number; open?: boolean; width?: number } {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw) as { top?: unknown; open?: unknown; width?: unknown }
    return {
      top: typeof parsed.top === 'number' ? parsed.top : undefined,
      open: parsed.open === true,
      width: typeof parsed.width === 'number' ? parsed.width : undefined,
    }
  } catch {
    return {}
  }
}

function saveState(): void {
  try {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ top: Math.round(top.value), open: open.value, width: Math.round(width.value) }),
    )
  } catch {
    /* 隐私模式下存不下也不影响本次使用 */
  }
}

function measure(): void {
  headerH.value = readHeaderHeight()
  maxWidth.value = Math.max(DRAWER_MIN, Math.min(DRAWER_MAX, Math.round(window.innerWidth * 0.6)))
}

/**
 * 抽屉宽度的唯一真源：写到根元素的 CSS 变量上。
 * 抽屉自身、图标让位（AssistantDock 内）与主内容让位（App.vue）都读这一个变量，
 * 拖拽时不需要任何 Vue 大范围重渲染。
 */
function applyWidth(): void {
  document.documentElement.style.setProperty('--assistant-drawer-w', `${width.value}px`)
}

/* —— 抽屉左边缘的宽度拖拽手柄 —— */
let resizePointer = -1

function onResizeDown(event: PointerEvent): void {
  if (event.button !== 0 && event.pointerType === 'mouse') return
  resizing.value = true
  resizePointer = event.pointerId
  const el = event.currentTarget as HTMLElement | null
  el?.setPointerCapture?.(event.pointerId)
  document.documentElement.classList.add('assistant-resizing')
  event.preventDefault()
}

function onResizeMove(event: PointerEvent): void {
  if (!resizing.value || event.pointerId !== resizePointer) return
  width.value = clampDrawerWidth(window.innerWidth - event.clientX, window.innerWidth)
  applyWidth()
}

function onResizeUp(event: PointerEvent): void {
  if (!resizing.value) return
  resizing.value = false
  const el = event.currentTarget as HTMLElement | null
  el?.releasePointerCapture?.(event.pointerId)
  document.documentElement.classList.remove('assistant-resizing')
  saveState()
}

function onResizeKeydown(event: KeyboardEvent): void {
  const step = 16
  if (event.key === 'ArrowLeft') {
    width.value = clampDrawerWidth(width.value + step, window.innerWidth)
  } else if (event.key === 'ArrowRight') {
    width.value = clampDrawerWidth(width.value - step, window.innerWidth)
  } else if (event.key === 'Home') {
    width.value = DRAWER_MIN
  } else if (event.key === 'End') {
    width.value = maxWidth.value
  } else {
    return
  }
  event.preventDefault()
  applyWidth()
  saveState()
}

function onResizeDoubleClick(): void {
  width.value = defaultDrawerWidth(window.innerWidth)
  applyWidth()
  saveState()
}

function toggle(): void {
  open.value = !open.value
  saveState()
}

function close(): void {
  if (!open.value) return
  open.value = false
  saveState()
}

/* —— 拖拽：只改竖直位置 —— */
let pressed = false
let pointerId = -1
let startY = 0
let startTop = 0
let moved = false

function onPointerDown(event: PointerEvent): void {
  if (event.button !== 0 && event.pointerType === 'mouse') return
  const el = dockEl.value
  if (!el) return
  pressed = true
  moved = false
  pointerId = event.pointerId
  startY = event.clientY
  startTop = top.value
  el.setPointerCapture?.(event.pointerId)
  event.preventDefault()
}

function onPointerMove(event: PointerEvent): void {
  if (!pressed || event.pointerId !== pointerId) return
  const dy = event.clientY - startY
  if (Math.abs(dy) > 0) dragging.value = true
  if (!isClick(0, dy)) moved = true
  top.value = clampTop(startTop + dy, window.innerHeight, headerH.value)
}

function onPointerUp(event: PointerEvent): void {
  if (!pressed || event.pointerId !== pointerId) return
  pressed = false
  dragging.value = false
  dockEl.value?.releasePointerCapture?.(event.pointerId)
  if (!moved) {
    toggle()
    return
  }
  saveState()
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Enter' && event.key !== ' ') return
  event.preventDefault()
  toggle()
}

function onResize(): void {
  measure()
  top.value = clampTop(top.value, window.innerHeight, headerH.value)
  /* 视口变窄后把抽屉夹回新的上限（1024 时上限是 614） */
  width.value = clampDrawerWidth(width.value, window.innerWidth)
  applyWidth()
}

function onEscape(event: KeyboardEvent): void {
  if (event.key === 'Escape') close()
}

onMounted(() => {
  measure()
  const saved = loadState()
  width.value = clampDrawerWidth(saved.width ?? defaultDrawerWidth(window.innerWidth), window.innerWidth)
  applyWidth()
  top.value = clampTop(
    saved.top ?? window.innerHeight - DOCK_SIZE - 24,
    window.innerHeight,
    headerH.value,
  )
  open.value = saved.open === true
  window.addEventListener('resize', onResize)
  window.addEventListener('keydown', onEscape)
})

/* 抽屉不再悬浮在内容上：打开时给主内容让出宽度（App.vue 里的 margin-right），
   由根元素上的一个类驱动，页面自己不用知道抽屉状态 */
watch(
  open,
  (value) => {
    document.documentElement.classList.toggle('assistant-drawer-open', value)
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  window.removeEventListener('keydown', onEscape)
  document.documentElement.classList.remove('assistant-drawer-open', 'assistant-resizing')
  document.documentElement.style.removeProperty('--assistant-drawer-w')
})
</script>

<template>
  <!-- 图标：fixed 在右侧，只有 top 会变；展开时向右让出抽屉宽度 -->
  <button
    ref="dockEl"
    type="button"
    class="assistant-dock"
    :class="{ 'is-open': open, 'is-dragging': dragging, 'is-resizing': resizing }"
    :style="{ top: `${top}px` }"
    :aria-expanded="open"
    aria-controls="assistant-panel"
    aria-label="智能助手"
    :title="open ? '收起助手' : '打开助手'"
    data-testid="assistant-dock"
    @pointerdown="onPointerDown"
    @pointermove="onPointerMove"
    @pointerup="onPointerUp"
    @pointercancel="onPointerUp"
    @keydown="onKeydown"
  >
    <img src="/mascot/assistant-avatar-navy.png" alt="" draggable="false" />
  </button>

  <aside
    id="assistant-panel"
    class="assistant-drawer"
    :class="{ 'is-open': open, 'is-resizing': resizing }"
    :style="drawerTopStyle"
    :aria-hidden="!open"
    aria-label="智能助手"
    data-testid="assistant-drawer"
  >
    <!-- 左边缘的宽度手柄：只能水平拖，上下贴满抽屉；键盘 ←/→ 也能调 -->
    <div
      class="assistant-drawer__resize"
      role="separator"
      aria-orientation="vertical"
      aria-label="调整助手侧栏宽度"
      :aria-valuenow="width"
      :aria-valuemin="DRAWER_MIN"
      :aria-valuemax="maxWidth"
      tabindex="0"
      title="拖动调整宽度（双击复位）"
      @pointerdown="onResizeDown"
      @pointermove="onResizeMove"
      @pointerup="onResizeUp"
      @pointercancel="onResizeUp"
      @keydown="onResizeKeydown"
      @dblclick="onResizeDoubleClick"
    />
    <header class="assistant-drawer__head">
      <span class="assistant-drawer__title">智能助手</span>
      <button type="button" class="assistant-drawer__close" aria-label="收起助手" @click="close">×</button>
    </header>
    <div class="assistant-drawer__body">
      <AssistantPanel mode="dock" />
    </div>
  </aside>
</template>

<style scoped>
.assistant-dock {
  position: fixed;
  right: 24px;
  z-index: 40;
  width: 56px;
  height: 56px;
  padding: 0;
  border: none;
  border-radius: 50%;
  background: transparent;
  cursor: grab;
  touch-action: none;
  filter: drop-shadow(0 6px 16px var(--aeg-color-accent-ball-glow));
  transition:
    right var(--aeg-motion-base) ease,
    top var(--aeg-motion-base) ease;
}
.assistant-dock.is-open {
  right: calc(var(--assistant-drawer-w, clamp(320px, 25vw, 520px)) + 12px);
}
.assistant-dock.is-dragging,
.assistant-dock.is-resizing {
  transition: none;
}
.assistant-dock.is-dragging {
  cursor: grabbing;
}
.assistant-dock img {
  width: 100%;
  height: 100%;
  display: block;
  border-radius: 50%;
  user-select: none;
  -webkit-user-drag: none;
}

.assistant-drawer {
  position: fixed;
  right: 0;
  bottom: 0;
  z-index: 39;
  display: flex;
  flex-direction: column;
  width: var(--assistant-drawer-w, clamp(320px, 25vw, 520px));
  border-left: 1px solid var(--aeg-color-glass-border);
  background: var(--aeg-color-glass-bg);
  backdrop-filter: blur(var(--aeg-glass-blur));
  box-shadow: var(--aeg-shadow-lg);
  transform: translateX(100%);
  visibility: hidden;
  transition:
    transform var(--aeg-motion-base) ease,
    visibility var(--aeg-motion-base) ease;
}
.assistant-drawer.is-resizing {
  transition: none;
}
.assistant-drawer.is-open {
  transform: translateX(0);
  visibility: visible;
}
/* 左边缘宽度手柄：8px 命中区，平时透明，hover/拖动/聚焦时亮一条 2px 主色线 */
.assistant-drawer__resize {
  position: absolute;
  top: 0;
  bottom: 0;
  left: -4px;
  z-index: 2;
  width: 8px;
  cursor: col-resize;
  touch-action: none;
  background: transparent;
}
.assistant-drawer__resize::after {
  content: '';
  position: absolute;
  top: 0;
  bottom: 0;
  left: 3px;
  width: 2px;
  border-radius: var(--aeg-radius-pill);
  background: transparent;
  transition: background-color var(--aeg-motion-base) ease;
}
.assistant-drawer__resize:hover::after,
.assistant-drawer__resize:focus-visible::after,
.assistant-drawer.is-resizing .assistant-drawer__resize::after {
  background: var(--aeg-color-brand);
}
.assistant-drawer__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--aeg-space-s3);
  padding: var(--aeg-space-s4);
  border-bottom: 1px solid var(--aeg-color-glass-border);
}
.assistant-drawer__title {
  font-size: var(--aeg-font-md);
  font-weight: var(--aeg-weight-bold);
  color: var(--aeg-color-text);
}
.assistant-drawer__close {
  width: 28px;
  height: 28px;
  padding: 0;
  border: 1px solid var(--aeg-color-glass-border);
  border-radius: var(--aeg-radius-pill);
  background: transparent;
  color: var(--aeg-color-text-secondary);
  font-size: var(--aeg-font-md);
  line-height: 1;
  cursor: pointer;
}
.assistant-drawer__close:hover {
  color: var(--aeg-color-text);
  background: var(--aeg-color-brand-soft);
}
.assistant-drawer__body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  /* 滚动交给 AssistantPanel 内部的会话区：抽屉本身不滚，输入区才不会被滚走 */
  overflow: hidden;
}

@media (prefers-reduced-motion: reduce) {
  .assistant-dock,
  .assistant-drawer {
    transition: none;
  }
}
</style>
