<script setup lang="ts">
/**
 * 态势总览的“静态三维地形 + 动态风险图层”。
 *
 * 底图是一张艺术化的高原三维切片（不是 GIS，不承诺经纬精度）：
 * 全屏铺图 + 一层 SVG/HTML 标记；标记位置来自 terrainAnchors.ts 的人工校准锚点，
 * 没校准的记录不上图（列表里如实标注），不用经纬度硬投影、也不随机撒点。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import type { RiskLevel } from '@/api/types'
import { RISK_COLORS, RISK_LABELS } from '@/api/types'
import type { SceneCell } from '@/components/dashboard/terrainAnchors'

const props = defineProps<{ cells: SceneCell[]; selectedId?: string | null }>()
const emit = defineEmits<{ (event: 'select', id: string): void }>()

const sceneRef = ref<HTMLElement | null>(null)
const imgRef = ref<HTMLImageElement | null>(null)
/** 图片元素自身在容器里的实际矩形（相对容器）：标记层与它同框才不会错位 */
const artBox = ref({ left: 0, top: 0, width: 0, height: 0 })

function measureArt(): void {
  const el = sceneRef.value
  const img = imgRef.value
  if (!el || !img) return
  const cw = el.clientWidth
  const ch = el.clientHeight
  if (!cw || !ch) return
  // 图片用 max-width/max-height 等比缩放（不再依赖 object-fit 的裁切/留白计算）：
  // 直接量 <img> 的真实盒子，标记层与它同框；容器再怎么换比例也不会裁掉地形。
  const sr = el.getBoundingClientRect()
  const ir = img.getBoundingClientRect()
  if (!ir.width || !ir.height) return
  artBox.value = { left: ir.left - sr.left, top: ir.top - sr.top, width: ir.width, height: ir.height }
}

let observer: ResizeObserver | null = null
onMounted(() => {
  measureArt()
  if (typeof ResizeObserver !== 'undefined') {
    observer = new ResizeObserver(measureArt)
    if (sceneRef.value) observer.observe(sceneRef.value)
    if (imgRef.value) observer.observe(imgRef.value)
  }
  // 图片解码完成后再量一次（首帧可能还是 0 尺寸）
  imgRef.value?.addEventListener('load', measureArt)
  window.addEventListener('resize', measureArt)
})
onBeforeUnmount(() => {
  observer?.disconnect()
  observer = null
  imgRef.value?.removeEventListener('load', measureArt)
  window.removeEventListener('resize', measureArt)
})

const placed = computed(() => props.cells.filter((cell) => cell.anchor))
const unplaced = computed(() => props.cells.filter((cell) => !cell.anchor))

/** 图例与标记同一份色：RISK_COLORS 是全局唯一真源 */
const legend = [
  { label: '高风险', color: RISK_COLORS[1] },
  { label: '中风险', color: RISK_COLORS[2] },
  { label: '低风险', color: RISK_COLORS[3] },
  { label: '监测点', color: 'var(--aeg-color-brand)' },
  { label: '无风险', color: 'var(--aeg-color-text-muted)' },
]

function isSelected(id: string): boolean {
  return props.selectedId === id
}

function colorOf(level: RiskLevel): string {
  return RISK_COLORS[level]
}
</script>

<template>
  <div ref="sceneRef" class="scene" data-testid="dashboard-terrain-scene">
    <img
      ref="imgRef"
      class="scene__art"
      src="/backgrounds/aegis-terrain-slab.png"
      alt="西藏高原三维地形示意（非 GIS 底图）"
      draggable="false"
      :style="{ width: 'auto', height: 'auto', maxWidth: '100%', maxHeight: '100%', objectFit: 'contain' }"
    />
    <div class="scene__veil" aria-hidden="true"></div>

    <!-- 标记层与图片的实际渲染框同尺寸：不同分辨率下 cover 的裁剪量变化，% 锚点仍对得上 -->
    <div
      class="scene__markers"
      :style="{
        left: `${artBox.left}px`,
        top: `${artBox.top}px`,
        width: `${artBox.width}px`,
        height: `${artBox.height}px`,
      }"
    >
      <button
        v-for="cell in placed"
        :key="cell.id"
        type="button"
        class="marker"
        :class="[`is-level-${cell.level}`, { 'is-selected': isSelected(cell.id) }]"
        :style="{ left: `${cell.anchor!.x}%`, top: `${cell.anchor!.y}%`, '--marker-color': colorOf(cell.level) }"
        :data-testid="`terrain-marker-${cell.id}`"
        :aria-pressed="isSelected(cell.id)"
        @click="emit('select', cell.id)"
      >
        <span class="marker__pulse" aria-hidden="true"></span>
        <span class="marker__dot" aria-hidden="true"></span>
        <span class="marker__label">{{ cell.region }} · {{ cell.hazard }}</span>
        <span class="marker__level">{{ RISK_LABELS[cell.level] }}</span>
      </button>
    </div>

    <p v-if="!placed.length" class="scene__hint">
      地形为示意底图；当前风险记录还没有配置场景锚点，所以图上没有落点。
    </p>

    <!-- 顶部状态 + 右上跳转：与设计稿的紧凑工具条对齐，不再叠 Cesium 的那套 HUD -->
    <div class="scene__hud scene__hud--left">
      <span class="scene__chip">风险落点 <b>{{ placed.length }}</b></span>
      <span v-if="unplaced.length" class="scene__chip">未定位 <b>{{ unplaced.length }}</b></span>
    </div>
    <div class="scene__hud scene__hud--right">
      <router-link class="scene__tool" to="/map" data-testid="terrain-open-map">一张图 →</router-link>
    </div>

    <!-- 左下风险图例：颜色与风险标记同一份 RISK_COLORS -->
    <div class="scene__legend" aria-label="风险等级图例">
      <span v-for="entry in legend" :key="entry.label" class="scene__legend-item">
        <i class="scene__legend-dot" :style="{ background: entry.color }" aria-hidden="true"></i>
        {{ entry.label }}
      </span>
    </div>
  </div>
</template>

<style scoped>
.scene {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  height: 100%;
  /* 0：容器多矮就多矮——绝不因为组件自己的最小高度超出卡片而被裁掉底边 */
  min-height: 0;
  overflow: hidden;
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-xl);
  background: var(--aeg-color-page);
}
/* 图片用 max-width/max-height 等比缩放：完整显示、不裁切、不拉伸；
   容器比例再宽也只是两侧留深蓝，绝不把地形切掉 */
.scene__art {
  position: relative;
  display: block;
  width: auto;
  height: auto;
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
  user-select: none;
  -webkit-user-drag: none;
}
/* 压一层深蓝雾：底图是亮色卫星感，直接铺会把周边卡片压成灰 */
.scene__veil {
  position: absolute;
  inset: 0;
  background:
    radial-gradient(120% 90% at 50% 40%, transparent 42%, color-mix(in srgb, var(--aeg-color-page) 62%, transparent) 100%),
    linear-gradient(to bottom, color-mix(in srgb, var(--aeg-color-page) 55%, transparent) 0%, transparent 22%);
  pointer-events: none;
}
/* 与图片 cover 后的渲染框同框：锚点百分比相对它换算，换分辨率不会跑偏 */
.scene__markers {
  position: absolute;
  z-index: 2;
  pointer-events: none;
}

.marker {
  position: absolute;
  z-index: 2;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 8px 4px 6px;
  pointer-events: auto;
  border: 1px solid color-mix(in srgb, var(--marker-color) 60%, transparent);
  border-radius: var(--aeg-radius-pill);
  background: color-mix(in srgb, var(--aeg-color-page) 78%, transparent);
  color: var(--aeg-color-text);
  font-size: var(--aeg-font-xs);
  line-height: var(--aeg-lh-snug);
  white-space: nowrap;
  cursor: pointer;
  transform: translate(-50%, -50%);
  transition:
    border-color var(--aeg-motion-base) ease,
    box-shadow var(--aeg-motion-base) ease,
    transform var(--aeg-motion-base) ease;
}
.marker:hover,
.marker:focus-visible {
  border-color: var(--marker-color);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--marker-color) 22%, transparent);
}
.marker.is-selected {
  border-color: var(--marker-color);
  box-shadow:
    0 0 0 3px color-mix(in srgb, var(--marker-color) 34%, transparent),
    0 8px 22px -12px var(--marker-color);
  transform: translate(-50%, -50%) scale(1.04);
}
.marker__pulse,
.marker__dot {
  flex: 0 0 auto;
  width: 8px;
  height: 8px;
  border-radius: var(--aeg-radius-pill);
  background: var(--marker-color);
}
.marker__pulse {
  position: absolute;
  left: 6px;
  width: 8px;
  height: 8px;
  opacity: 0.5;
  animation: marker-pulse 2.4s ease-out infinite;
}
.marker.is-level-1 .marker__pulse,
.marker.is-level-2 .marker__pulse {
  animation-duration: 1.6s;
}
.marker__label {
  font-weight: var(--aeg-weight-semibold);
}
.marker__level {
  color: color-mix(in srgb, var(--marker-color) 80%, var(--aeg-color-text));
}
@keyframes marker-pulse {
  0% {
    transform: scale(1);
    opacity: 0.5;
  }
  70% {
    transform: scale(3.4);
    opacity: 0;
  }
  100% {
    transform: scale(3.4);
    opacity: 0;
  }
}

.scene__hint {
  position: absolute;
  left: 50%;
  bottom: 16px;
  z-index: 2;
  max-width: 90%;
  margin: 0;
  padding: 6px 12px;
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-pill);
  background: color-mix(in srgb, var(--aeg-color-page) 82%, transparent);
  color: var(--aeg-color-text-muted);
  font-size: var(--aeg-font-xs);
  transform: translateX(-50%);
}
.scene__hint--corner {
  left: auto;
  right: 16px;
  transform: none;
}

/* 顶部工具条与图例：压在画布上，层级高于标记层 */
.scene__hud {
  position: absolute;
  top: 12px;
  z-index: 3;
  display: inline-flex;
  align-items: center;
  gap: var(--aeg-space-s2);
}
.scene__hud--left {
  left: 12px;
}
.scene__hud--right {
  right: 12px;
}
.scene__chip,
.scene__tool {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 10px;
  border: 1px solid var(--aeg-color-glass-border);
  border-radius: var(--aeg-radius-pill);
  background: color-mix(in srgb, var(--aeg-color-page) 78%, transparent);
  color: var(--aeg-color-text-secondary);
  font-size: var(--aeg-font-xs);
  text-decoration: none;
}
.scene__chip b {
  color: var(--aeg-color-text);
  font-variant-numeric: tabular-nums;
}
.scene__tool:hover {
  border-color: var(--aeg-color-brand);
  color: var(--aeg-color-text);
}
.scene__legend {
  position: absolute;
  left: 12px;
  bottom: 12px;
  z-index: 3;
  display: flex;
  flex-wrap: wrap;
  gap: var(--aeg-space-s1) var(--aeg-space-s3);
  padding: 4px 10px;
  border: 1px solid var(--aeg-color-glass-border);
  border-radius: var(--aeg-radius-pill);
  background: color-mix(in srgb, var(--aeg-color-page) 78%, transparent);
  color: var(--aeg-color-text-muted);
  font-size: var(--aeg-font-xs);
}
.scene__legend-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.scene__legend-dot {
  width: 7px;
  height: 7px;
  border-radius: var(--aeg-radius-pill);
}

/* 脉冲是装饰：减动效偏好下停掉，避免高频呼吸灯 */
@media (prefers-reduced-motion: reduce) {
  .marker__pulse {
    animation: none;
  }
}
</style>
