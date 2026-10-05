<script setup lang="ts">
/**
 * 定义级编辑：名称/描述、连线条件、本地校验清单，以及服务端已存版本的摘要。
 * 服务端定义列表只回摘要（workflow_api.py:113-130），不含节点与连线，
 * 所以"打开"要走单条定义的出口（GET /definitions/{id}）把全文取回画布。
 */
import { computed } from 'vue'

import { confirmDiscardUnsaved } from './confirmDiscard'
import { confirmArchive } from './confirmArchive'

import type { DefinitionSummary } from '@/api/workflow'
import { useWorkflowStore } from '@/stores/workflow'
import { definitionStatusLabel, edgeId, MAX_DEFINITION_NAME_CHARS, MAX_DESCRIPTION_CHARS, type EdgeDef } from '@/utils/graph'

const store = useWorkflowStore()

/** 打开一份已存定义：会整体覆盖画布，有未保存改动时先确认。 */
function open(workflowId: string): void {
  const proceed = (): void => {
    void store.openDefinition(workflowId)
  }
  if (store.isDirty) confirmDiscardUnsaved('打开这份定义', proceed)
  else proceed()
}

/** 归档要确认（改了服务端状态、会停掉按名字取用的自动化）；取消归档是安全的反向动作，直接做。 */
function toggleArchive(row: DefinitionSummary): void {
  if (row.status === 'archived') {
    void store.restoreDefinition(row.workflow_id)
    return
  }
  confirmArchive(row.name, row.version, () => {
    void store.archiveDefinition(row.workflow_id)
  })
}

/** 边条件与上游节点输出的 branch 名比对（engine.py:344-350），故只能提示、不能替用户猜。 */
const CONDITION_HINT = '留空表示无条件放行；填分支名（如 triggered / level_2）表示仅该分支命中时执行。'

interface EdgeRow {
  id: string
  source: string
  target: string
  condition: string
}

const def = computed(() => store.current)

const edges = computed<EdgeRow[]>(() => {
  const current = def.value
  if (current === null) return []
  return current.edges.map((edge: EdgeDef, index) => ({ id: edgeId(index, edge), ...edge }))
})

const nameOf = (nodeId: string): string => {
  const node = def.value?.nodes.find((item) => item.node_id === nodeId)
  return node?.name === '' || node?.name === undefined ? nodeId : `${node.name}（${nodeId}）`
}

function onName(event: Event): void {
  store.setMeta({ name: (event.target as HTMLInputElement).value })
}

function onDescription(event: Event): void {
  store.setMeta({ description: (event.target as HTMLTextAreaElement).value })
}
</script>

<template>
  <section v-if="def !== null" class="wf-def">
    <h3 class="wf-def__title">工作流定义</h3>
    <label class="wf-def__row">
      <span>名称</span>
      <input class="wf-def__input" type="text" :value="def.name" :maxlength="MAX_DEFINITION_NAME_CHARS" @change="onName" />
    </label>
    <label class="wf-def__row">
      <span>描述</span>
      <textarea class="wf-def__input" rows="2" :value="def.description" :maxlength="MAX_DESCRIPTION_CHARS" @change="onDescription" />
    </label>
    <p class="wf-def__identity">
      <code>{{ def.workflow_id === '' ? '未保存' : def.workflow_id }}</code>
      <span>v{{ def.version }}</span>
      <span>{{ def.nodes.length }} 节点 / {{ def.edges.length }} 连线</span>
    </p>

    <h4 class="wf-def__subtitle">连线（{{ edges.length }}）</h4>
    <p v-if="edges.length === 0" class="wf-def__empty">从节点右侧圆点拖到目标节点左侧圆点即可连线。</p>
    <ul v-else class="wf-def__edges">
      <li v-for="edge in edges" :key="edge.id" class="wf-def__edge">
        <div class="wf-def__edge-endpoints">
          <span :title="nameOf(edge.source)">{{ edge.source }}</span>
          <span aria-hidden="true">→</span>
          <span :title="nameOf(edge.target)">{{ edge.target }}</span>
        </div>
        <div class="wf-def__edge-condition">
          <input
            class="wf-def__input"
            type="text"
            :value="edge.condition"
            placeholder="边条件（分支名）"
            maxlength="64"
            @change="store.updateEdgeCondition(edge.id, ($event.target as HTMLInputElement).value)"
          />
          <button type="button" class="wf-def__button wf-def__button--danger" @click="store.removeEdge(edge.id)">删除</button>
        </div>
        <p class="wf-def__hint">{{ CONDITION_HINT }}</p>
      </li>
    </ul>

    <h4 class="wf-def__subtitle">本地校验</h4>
    <p class="wf-def__hint">以下仅为提交前拦截，服务端仍会复核类型注册、重名版本与运行期配置。</p>
    <ul v-if="store.validationErrors.length > 0" class="wf-def__errors">
      <li v-for="message in store.validationErrors" :key="message">{{ message }}</li>
    </ul>
    <p v-else class="wf-def__ok">校验通过，可保存。</p>

    <h4 class="wf-def__subtitle">服务端已存定义</h4>
    <ul class="wf-def__list">
      <li v-for="row in store.definitions" :key="row.workflow_id" class="wf-def__list-item">
        <span>{{ row.name }} v{{ row.version }}</span>
        <span class="wf-def__muted">{{ row.node_count }} 节点 / {{ row.edge_count }} 连线</span>
        <span class="wf-def__muted">{{ definitionStatusLabel(row.status) }}</span>
        <!-- 打开排在归档前面：这是一条能存不能开的链路修好后的样子，
             顺序也是提醒——先能拿回来编辑，再谈要不要收走它 -->
        <button type="button" class="wf-def__button" :data-testid="`open-${row.workflow_id}`" @click="open(row.workflow_id)">打开</button>
        <!-- 归档与取消归档是同一个开关的两面：一按就改了服务端状态的动作要先问一句，
             而把它撤回的那一下不该再拦人 -->
        <button
          type="button"
          class="wf-def__button"
          :data-testid="`archive-${row.workflow_id}`"
          @click="toggleArchive(row)"
        >
          {{ row.status === 'archived' ? '取消归档' : '归档' }}
        </button>
      </li>
    </ul>
    <p v-if="store.definitions.length === 0" class="wf-def__empty">尚无已保存定义。</p>
  </section>
</template>

<style scoped>
.wf-def {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 12px;
  border-top: 1px solid #f0f0f0;
}
.wf-def__title,
.wf-def__subtitle {
  margin: 0;
  font-size: 13px;
}
.wf-def__subtitle {
  margin-top: 6px;
  color: #595959;
  font-size: 12px;
}
.wf-def__row {
  display: flex;
  flex-direction: column;
  gap: 2px;
  color: #595959;
  font-size: 12px;
}
.wf-def__input {
  padding: 2px 6px;
  border: 1px solid #d9d9d9;
  border-radius: 4px;
  font-size: 12px;
}
.wf-def__identity {
  display: flex;
  gap: 8px;
  margin: 0;
  color: #5a6072;
  font-size: 11px;
}
.wf-def__edges,
.wf-def__list,
.wf-def__errors {
  margin: 0;
  padding: 0;
  list-style: none;
}
.wf-def__errors {
  color: #cf1322;
  font-size: 12px;
}
.wf-def__edge,
.wf-def__list-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 6px 0;
  border-bottom: 1px dashed #f0f0f0;
}
.wf-def__list-item {
  /* 与运行实例那一列同一个列表行习语（悬停上雾、圆角 10），
     不再是"虚线分隔的表行"——这一列是可以点开的入口，不是读数。 */
  flex-direction: row;
  gap: 6px;
  align-items: center;
  padding: 7px 10px;
  border: 1px solid transparent;
  border-radius: 10px;
  font-size: 12px;
  transition: background 0.15s ease, border-color 0.15s ease;
}
.wf-def__list-item:hover {
  background: rgba(37, 99, 235, 0.05);
  border-color: rgba(37, 99, 235, 0.18);
}
/* 名字那几段是可伸缩的，两颗按钮不能：原先名字一长就把"打开/归档"挤成两行
   （1440 真机截图里就是"打/开"竖排），这里让文字自己折、按钮保持一行。
   计数与状态那两段是词组，折在中间读起来像坏了，只允许名字折。 */
.wf-def__list-item {
  flex-wrap: wrap;
}
.wf-def__list-item > span {
  min-width: 0;
  white-space: nowrap;
}
.wf-def__list-item > span:first-child {
  flex: 1 1 auto;
  white-space: normal;
  overflow-wrap: anywhere;
}
.wf-def__edge-endpoints {
  display: flex;
  gap: 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
}
.wf-def__edge-condition {
  display: flex;
  gap: 4px;
}
.wf-def__button {
  flex: 0 0 auto;
  padding: 2px 8px;
  border: 1px solid #d9d9d9;
  border-radius: 4px;
  background: #fff;
  font-size: 12px;
  white-space: nowrap;
  cursor: pointer;
}
.wf-def__button--danger {
  border-color: #ffa39e;
  color: #cf1322;
}
.wf-def__hint,
.wf-def__empty,
.wf-def__ok,
.wf-def__muted {
  margin: 0;
  color: #5a6072;
  font-size: 11px;
}
.wf-def__ok {
  color: #389e0d;
}
</style>
