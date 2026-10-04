<script setup lang="ts">
/**
 * 工作流编排画布页：左侧节点面板 / 中间 Vue Flow 画布 / 右侧检查器与实例控制。
 *
 * 页面本身只做排版与副作用编排：模型在 stores/workflow，图换算与校验在 utils/graph，
 * 画布事件粘合在 useWorkflowCanvas，组件层按"类别"而非"类型"拆分（16 类 → 4 个卡片组件）。
 */
import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import '@vue-flow/minimap/dist/style.css'
import '@vue-flow/controls/dist/style.css'

import { VueFlow } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import { MiniMap } from '@vue-flow/minimap'
import { message } from 'ant-design-vue'
import { onMounted, onUnmounted } from 'vue'

import { useWorkflowStore } from '@/stores/workflow'

import DefinitionInspector from '@/components/workflow/DefinitionInspector.vue'
import NodeInspector from '@/components/workflow/NodeInspector.vue'
import NodePalette from '@/components/workflow/NodePalette.vue'
import RuntimePanel from '@/components/workflow/RuntimePanel.vue'
import { WORKFLOW_NODE_TYPES } from '@/components/workflow/nodeComponents'
import { confirmDiscardUnsaved } from '@/components/workflow/confirmDiscard'
import { useWorkflowCanvas } from '@/components/workflow/useWorkflowCanvas'

const store = useWorkflowStore()
const { view, onConnect, onNodeClick, onPaneClick, onNodeDragStart, onNodeDragStop, onDragOver, onDrop } =
  useWorkflowCanvas(store)

async function bootstrap(): Promise<void> {
  if (store.current === null) store.resetDefinition()
  try {
    await Promise.all([store.loadNodeTypes(), store.loadDefinitions(), store.loadInstances()])
  } catch (caught) {
    message.error(store.describeFailure(caught))
  }
}

async function save(): Promise<void> {
  // 传入当前画布视图：所见即所存（graphToDef 逆映射），避免模型与视图各说一套。
  if (await store.saveDefinition(view.value)) message.success('定义已保存并生成新版本')
  else if (store.error !== null) message.error(store.error)
}

async function createDraft(): Promise<void> {
  const proceed = (): void => {
    store.resetDefinition()
    message.success('已新建空白画布')
  }
  // 有未保存改动时先确认：一按就清空、只弹一句 toast 的话，重画的成本全在值班员身上
  if (store.isDirty) confirmDiscardUnsaved('新建画布', proceed)
  else proceed()
}

onMounted(bootstrap)
onUnmounted(() => store.stopPolling())
</script>

<template>
  <div class="wf">
    <header class="wf__bar">
      <h2 class="wf__name">{{ store.current?.name ?? '工作流编排' }}</h2>
      <span class="wf__version">v{{ store.current?.version ?? 1 }}</span>
      <a-button type="primary" size="small" :loading="store.loading" :disabled="!store.canSave" @click="save">
        保存定义
      </a-button>
      <a-button size="small" @click="createDraft">新建画布</a-button>
      <a-button size="small" @click="store.autoLayout()">自动布局</a-button>
      <a-button size="small" @click="store.refreshInstance">刷新实例</a-button>
      <span v-if="store.polling" class="wf__polling">实例状态轮询中</span>
    </header>

    <p v-if="store.error !== null" class="wf__error">{{ store.error }}</p>
    <p v-else-if="store.validationErrors.length > 0" class="wf__warn">
      画布尚有 {{ store.validationErrors.length }} 处待修正，详见右下"本地校验"。
    </p>

    <div class="wf__body">
      <NodePalette class="wf__left" />

      <div class="wf__canvas" @drop="onDrop" @dragover="onDragOver">
        <VueFlow
          :nodes="view.nodes"
          :edges="view.edges"
          :node-types="WORKFLOW_NODE_TYPES"
          :edges-updatable="false"
          :delete-key-code="null"
          :min-zoom="0.2"
          :max-zoom="2"
          :default-viewport="{ x: 40, y: 40, zoom: 0.9 }"
          fit-view-on-init
          @connect="onConnect"
          @node-click="onNodeClick"
          @pane-click="onPaneClick"
          @node-drag-start="onNodeDragStart"
          @node-drag-stop="onNodeDragStop"
        >
          <Background pattern-color="#d9d9d9" :gap="16" />
          <MiniMap />
          <Controls />
        </VueFlow>
      </div>

      <aside class="wf__right">
        <NodeInspector />
        <DefinitionInspector />
        <RuntimePanel />
      </aside>
    </div>
  </div>
</template>

<style scoped>
.wf {
  display: flex;
  flex-direction: column;
  gap: 8px;
  height: 100%;
  min-height: 0;
}
.wf__bar {
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 8px 12px;
  border: 1px solid #f0f0f0;
  border-radius: 6px;
  background: #fff;
}
.wf__name {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
}
.wf__version {
  color: #8c8c8c;
  font-size: 12px;
}
.wf__polling {
  margin-left: auto;
  color: #13c2c2;
  font-size: 12px;
}
.wf__error,
.wf__warn {
  margin: 0;
  padding: 6px 12px;
  border-radius: 4px;
  font-size: 12px;
}
.wf__error {
  background: #fff2f0;
  color: #cf1322;
}
.wf__warn {
  background: #fffbe6;
  color: #874d00;
}
.wf__body {
  display: grid;
  grid-template-columns: 208px minmax(0, 1fr) 340px;
  gap: 8px;
  flex: 1;
  min-height: 0;
  height: calc(100vh - 220px);
}
.wf__left,
.wf__right {
  border: 1px solid #f0f0f0;
  border-radius: 6px;
  background: #fff;
  overflow-y: auto;
}
.wf__right {
  display: flex;
  flex-direction: column;
}
.wf__canvas {
  position: relative;
  border: 1px solid #f0f0f0;
  border-radius: 6px;
  background: #fff;
  min-height: 0;
  /* 网格项默认 min-width:auto：轨道写了 minmax(0,1fr) 也没用，画布仍按内容最小宽撑住 */
  min-width: 0;
}

/*
 * 窄屏（值班指挥员的平板，768–1000px 这一档）放不下"面板 208 + 画布 + 检查器 340"三列：
 * 去掉导航栏 232px 后实际可用只有 ~520px，硬排三列会把整页撑出横向滚动，
 * 右侧检查器（改参数、签工单都在那儿）被推到屏幕外看不见。
 * 这里不做"缩成一条缝"的妥协：三列改两行，画布保住宽度，检查器整行放在下面。
 */
@media (max-width: 1000px) {
  .wf__body {
    grid-template-columns: minmax(160px, 208px) minmax(0, 1fr);
    grid-template-areas:
      'left canvas'
      'right right';
    height: auto;
  }
  .wf__left {
    grid-area: left;
  }
  .wf__canvas {
    grid-area: canvas;
    min-height: 60vh;
  }
  .wf__right {
    grid-area: right;
  }
}
</style>
