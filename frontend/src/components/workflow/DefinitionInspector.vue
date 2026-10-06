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
        <span class="wf-def__name" :title="row.name">{{ row.name }}</span>
        <span class="wf-def__ver">v{{ row.version }}</span>
        <span class="wf-def__muted wf-def__meta">{{ row.node_count }} 节点 / {{ row.edge_count }} 连线</span>
        <span class="wf-def__muted wf-def__state">{{ definitionStatusLabel(row.status) }}</span>
        <!-- 打开排在归档前面：这是一条能存不能开的链路修好后的样子，
             顺序也是提醒——先能拿回来编辑，再谈要不要收走它 -->
        <button type="button" class="wf-def__button wf-def__button--open" :data-testid="`open-${row.workflow_id}`" @click="open(row.workflow_id)">打开</button>
        <!-- 归档与取消归档是同一个开关的两面：一按就改了服务端状态的动作要先问一句，
             而把它撤回的那一下不该再拦人 -->
        <button
          type="button"
          class="wf-def__button wf-def__button--archive"
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
/**
 * 「服务端已存定义」每一行。
 *
 * 之前是 `flex-wrap: wrap`（为了修更早的"打/开竖排"），结果折到哪一行没人管：
 * 真机 @1440 量到行高 62px，而两颗按钮落在**不同行的不同位置**
 * （"打开" top=599 靠右、"归档" top=625 靠左下）。322px 的右栏里
 * 名字 + "N 节点 / M 连线" + 状态 + 两颗按钮（各 42px）+ 间距 ≈ 347px，
 * 一行**装不下**——所以问题不是"该不该折"，而是"折得有没有规矩"。
 * 这里改成两行网格：第一行 名字 + 打开，第二行 计数 + 状态 + 归档，
 * 两颗按钮右缘对齐成一列（扫一眼就能比长短），行高由内容决定而不是由 wrap 决定。
 */
.wf-def__list-item {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;
  column-gap: 6px;
  row-gap: 2px;
  align-items: center;
  padding: 6px 10px;
  border: 1px solid transparent;
  border-radius: 10px;
  font-size: 12px;
  /* 0.15s 是 NexusMind 全站的微动效基准（284 处 `transition: all .15s`） */
  transition: background 0.15s ease, border-color 0.15s ease;
}
.wf-def__list-item:hover {
  background: rgba(37, 99, 235, 0.05);
  border-color: rgba(37, 99, 235, 0.18);
}
.wf-def__name {
  grid-area: 1 / 1 / 2 / 2;
  min-width: 0;
  /* 名字单行收尾、全文进 title——和画布节点卡同一套口径（长标题不许把行撑高，
     否则一列长短不齐，"扫一眼比数量"就失效了）。真机量到一份长名字的定义把行撑到 63px。 */
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.wf-def__ver {
  grid-area: 1 / 2 / 2 / 3;
  color: #5a6072;
  white-space: nowrap;
}
.wf-def__meta {
  grid-area: 2 / 1 / 3 / 2;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.wf-def__state {
  grid-area: 2 / 2 / 3 / 3;
  white-space: nowrap;
}
.wf-def__button--open {
  grid-area: 1 / 3 / 2 / 4;
  justify-self: end;
}
.wf-def__button--archive {
  grid-area: 2 / 3 / 3 / 4;
  justify-self: end;
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
