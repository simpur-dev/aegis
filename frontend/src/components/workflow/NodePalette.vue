<script setup lang="ts">
/**
 * 左侧节点面板：16 类节点按类别分组，支持拖放到画布或直接点击追加。
 * 展示不依赖后端（注册表已含中文名与描述），node-types 接口返回后仅替换描述文案。
 */
import { computed } from 'vue'

import { useWorkflowStore } from '@/stores/workflow'
import { nextNodeId, type NodeType } from '@/utils/graph'

import { createNodeDef, NODE_DRAG_MIME, NODE_META, PALETTE_GROUPS } from './registry'

const store = useWorkflowStore()

const groups = computed(() =>
  PALETTE_GROUPS.map((group) => ({
    ...group,
    items: group.types.map((type) => ({
      type,
      label: NODE_META[type].label,
      description: store.descriptionByType[type] ?? NODE_META[type].description,
    })),
  })),
)

function append(type: NodeType): void {
  const def = store.current
  if (def === null) return
  store.addNode(createNodeDef(type, nextNodeId(def, type)))
}

function onDragStart(event: DragEvent, type: NodeType): void {
  event.dataTransfer?.setData(NODE_DRAG_MIME, type)
  if (event.dataTransfer !== null && event.dataTransfer !== undefined) event.dataTransfer.effectAllowed = 'move'
}
</script>

<template>
  <aside class="wf-palette">
    <h3 class="wf-palette__title">节点面板</h3>
    <section v-for="group in groups" :key="group.category" class="wf-palette__group">
      <h4 class="wf-palette__group-title">{{ group.label }}</h4>
      <button
        v-for="item in group.items"
        :key="item.type"
        class="wf-palette__item"
        type="button"
        draggable="true"
        :title="item.description"
        @dragstart="onDragStart($event, item.type)"
        @click="append(item.type)"
      >
        <span class="wf-palette__label">{{ item.label }}</span>
        <span class="wf-palette__type">{{ item.type }}</span>
      </button>
    </section>
    <p class="wf-palette__tip">拖拽或点击即可加入画布；选中节点后可在右侧改参数。</p>
  </aside>
</template>

<style scoped>
.wf-palette {
  display: flex;
  flex-direction: column;
  gap: 10px;
  height: 100%;
  padding: 12px;
  overflow-y: auto;
  border-right: 1px solid #f0f0f0;
  background: #fafafa;
}
.wf-palette__title {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
}
.wf-palette__group {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.wf-palette__group-title {
  margin: 6px 0 2px;
  color: #5a6072;
  font-size: 12px;
  font-weight: 500;
}
.wf-palette__item {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 6px;
  padding: 6px 8px;
  border: 1px solid #d9d9d9;
  border-radius: 4px;
  background: #fff;
  color: #262626;
  font-size: 12px;
  text-align: left;
  cursor: grab;
}
.wf-palette__item:hover {
  border-color: #1677ff;
}
.wf-palette__type {
  color: #5a6072;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
}
.wf-palette__tip {
  margin: auto 0 0;
  color: #5a6072;
  font-size: 11px;
}
</style>
