<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'

import { echarts, type Chart, type ChartOption, type CoreOption } from '@/components/echarts'

const props = defineProps<{ option: ChartOption; height?: string }>()

const host = ref<HTMLElement | null>(null)
const chart = shallowRef<Chart | null>(null)
let observer: ResizeObserver | null = null

function render(): void {
  if (!chart.value) return
  // notMerge=true：切换指标时不残留上一次的 series
  chart.value.setOption(props.option as CoreOption, true)
}

onMounted(() => {
  if (!host.value) return
  chart.value = echarts.init(host.value)
  render()
  observer = new ResizeObserver(() => chart.value?.resize())
  observer.observe(host.value)
})

onBeforeUnmount(() => {
  observer?.disconnect()
  chart.value?.dispose()
  chart.value = null
})

watch(() => props.option, render)
</script>

<template>
  <div ref="host" :style="{ height: props.height ?? '320px', width: '100%' }" />
</template>
