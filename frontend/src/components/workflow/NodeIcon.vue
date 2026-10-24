<script setup lang="ts">
/** 节点类型图标：字形表在 `presentation.ts`，这里只负责把路径画出来。 */
import { computed } from 'vue'

import { NODE_GLYPHS, resolveNodePresentation } from './presentation'

const props = withDefaults(defineProps<{ type: string; size?: number }>(), { size: 16 })

const glyph = computed(() => resolveNodePresentation(props.type).glyph)
const paths = computed(() => NODE_GLYPHS[glyph.value])
</script>

<template>
  <svg
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.8"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <path v-for="(d, index) in paths" :key="index" :d="d" />
  </svg>
</template>
