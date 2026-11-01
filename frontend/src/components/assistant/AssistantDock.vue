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

import { clampTop, DOCK_SIZE, isClick } from '@/components/assistant/dock'

const props = defineProps<{ immersive?: boolean }>()

const STORAGE_KEY = 'aegis.assistant.dock'

const dockEl = ref<HTMLButtonElement | null>(null)
const open = ref(false)
const dragging = ref(false)
const top = ref(0)
const headerH = ref(68)

const headerVar = computed(() => (props.immersive ? '--aeg-editor-header-height' : '--aeg-header-height'))

/** 抽屉宽度（与 CSS 的 clamp 保持一致，供图标让位时使用；实际位移由 CSS 算） */
const drawerTopStyle = computed(() => ({ top: `${headerH.value}px` }))

function readHeaderHeight(): number {
  const raw = getComputedStyle(document.documentElement).getPropertyValue(headerVar.value).trim()
  const px = Number.parseFloat(raw)
  return Number.isFinite(px) && px > 0 ? px : 68
}

function loadState(): { top?: number; open?: boolean } {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw) as { top?: unknown; open?: unknown }
    return {
      top: typeof parsed.top === 'number' ? parsed.top : undefined,
      open: parsed.open === true,
    }
  } catch {
    return {}
  }
}

function saveState(): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ top: Math.round(top.value), open: open.value }))
  } catch {
    /* 隐私模式下存不下也不影响本次使用 */
  }
}

function measure(): void {
  headerH.value = readHeaderHeight()
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
}

function onEscape(event: KeyboardEvent): void {
  if (event.key === 'Escape') close()
}

onMounted(() => {
  measure()
  const saved = loadState()
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
  document.documentElement.classList.remove('assistant-drawer-open')
})
</script>

<template>
  <!-- 图标：fixed 在右侧，只有 top 会变；展开时向右让出抽屉宽度 -->
  <button
    ref="dockEl"
    type="button"
    class="assistant-dock"
    :class="{ 'is-open': open, 'is-dragging': dragging }"
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
    :class="{ 'is-open': open }"
    :style="drawerTopStyle"
    :aria-hidden="!open"
    aria-label="智能助手"
    data-testid="assistant-drawer"
  >
    <header class="assistant-drawer__head">
      <span class="assistant-drawer__title">智能助手</span>
      <button type="button" class="assistant-drawer__close" aria-label="收起助手" @click="close">×</button>
    </header>
    <div class="assistant-drawer__body">
      <div class="assistant-drawer__empty">
        <img src="/mascot/assistant-avatar-navy.png" alt="" aria-hidden="true" />
        <p>侧栏内容待接入</p>
        <span>这里之后会放对话与快捷指令；现在只验证抽屉的开合与图标让位。</span>
      </div>
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
  right: calc(clamp(320px, 25vw, 520px) + 12px);
}
.assistant-dock.is-dragging {
  cursor: grabbing;
  transition: none;
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
  width: clamp(320px, 25vw, 520px);
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
.assistant-drawer.is-open {
  transform: translateX(0);
  visibility: visible;
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
  overflow-y: auto;
  padding: var(--aeg-space-s4);
}
.assistant-drawer__empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--aeg-space-s2);
  margin-top: 12vh;
  text-align: center;
  color: var(--aeg-color-text-secondary);
}
.assistant-drawer__empty img {
  width: 72px;
  height: 72px;
  border-radius: 50%;
  opacity: 0.9;
}
.assistant-drawer__empty p {
  margin: 0;
  font-size: var(--aeg-font-md);
  font-weight: var(--aeg-weight-semibold);
  color: var(--aeg-color-text);
}
.assistant-drawer__empty span {
  max-width: 30ch;
  font-size: var(--aeg-font-sm);
  line-height: var(--aeg-lh-normal);
}

@media (prefers-reduced-motion: reduce) {
  .assistant-dock,
  .assistant-drawer {
    transition: none;
  }
}
</style>
