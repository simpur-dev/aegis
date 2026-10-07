<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'

import { echarts, type Chart, type ChartOption, type CoreOption } from '@/components/echarts'
import { chartThemeName } from '@/theme/chart'
import { useTheme } from '@/theme/useTheme'

const props = defineProps<{ option: ChartOption; height?: string }>()

const host = ref<HTMLElement | null>(null)
const chart = shallowRef<Chart | null>(null)
let observer: ResizeObserver | null = null

/**
 * 主题名跟着 `resolved` 走。ECharts 的 theme 只能在 `init` 时给一次（实例上没有换主题的口子），
 * 所以翻转的代价是**重建实例**而不是重设 option——这也正是"画布读不到 CSS 变量"的直接后果。
 * 记自己挂过的主题名而不是问实例：`chart.getTheme()` 返回的是内部对象，比不出身份。
 */
const { resolved } = useTheme()
const themeName = computed(() => chartThemeName[resolved.value])
let mountedTheme = ''

function mount(): void {
  if (!host.value) return
  chart.value = echarts.init(host.value, themeName.value)
  mountedTheme = themeName.value
  render()
}

function render(): void {
  if (!chart.value) return
  // notMerge=true：切换指标时不残留上一次的 series
  chart.value.setOption(props.option as CoreOption, true)
}

onMounted(() => {
  if (!host.value) return
  mount()
  observer = new ResizeObserver(() => chart.value?.resize())
  observer.observe(host.value)
})

onBeforeUnmount(() => {
  observer?.disconnect()
  chart.value?.dispose()
  chart.value = null
})

watch(() => props.option, render)
watch(themeName, (next) => {
  if (next === mountedTheme || !host.value) return
  chart.value?.dispose()
  mount()
})
</script>

<template>
  <div ref="host" :data-theme="resolved" :style="{ height: props.height ?? '320px', width: '100%' }" />
</template>
