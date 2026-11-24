<script setup lang="ts">
/**
 * 定义级编辑：名称/描述、连线条件、本地校验清单，以及服务端已存版本的摘要。
 * 服务端定义列表只回摘要（workflow_api.py:113-130），不含节点与连线，
 * 所以"打开"要走单条定义的出口（GET /definitions/{id}）把全文取回画布。
 */
import { computed, ref } from 'vue'
import { ApartmentOutlined, SearchOutlined } from '@ant-design/icons-vue'

import FreshnessBar from '@/components/FreshnessBar.vue'
import { confirmDiscardUnsaved } from './confirmDiscard'
import { confirmArchive } from './confirmArchive'

import type { DefinitionSummary } from '@/api/workflow'
import { definitionStatusLabel, edgeId, MAX_DEFINITION_NAME_CHARS, MAX_DESCRIPTION_CHARS, type EdgeDef } from '@/utils/graph'

import { useEditorContext } from './editor/context'

const { catalog, document: documentStore, run } = useEditorContext()

const STANDARD_CHAIN_NAME = '标准五段处置链路'
const query = ref('')
const showArchived = ref(false)
const archivedCount = computed(() => catalog.definitions.filter((row) => row.status === 'archived').length)
const visibleDefinitions = computed(() => {
  const keyword = query.value.trim().toLocaleLowerCase()
  return catalog.definitions
    .filter((row) => showArchived.value || row.status !== 'archived')
    .filter((row) => !keyword || row.name.toLocaleLowerCase().includes(keyword) || row.workflow_id.toLowerCase().includes(keyword))
    .sort((a, b) => Number(b.name === STANDARD_CHAIN_NAME) - Number(a.name === STANDARD_CHAIN_NAME))
})

/** 系统链路打开发布快照，其余定义有 lineage 时打开草稿。 */
function open(row: DefinitionSummary): void {
  const proceed = async (): Promise<void> => {
    if (!row.lineage_id || row.name === STANDARD_CHAIN_NAME) {
      if (await documentStore.openVersion(row.workflow_id)) run.leaveInstance({ kind: 'version', workflowId: row.workflow_id })
    } else if (await documentStore.openLineage(row.lineage_id)) {
      run.leaveInstance({ kind: 'draft' })
    }
  }
  if (documentStore.hasUnsavedChanges) confirmDiscardUnsaved('打开这份定义', proceed)
  else void proceed()
}

/** 版本恢复会把那一版写成草稿（服务端状态），画布上有改动时先确认。 */
function restore(row: { workflow_id: string; version: number }): void {
  const proceed = async (): Promise<void> => {
    if (await documentStore.restoreVersion(row.workflow_id)) run.leaveInstance({ kind: 'draft' })
  }
  if (documentStore.hasUnsavedChanges) confirmDiscardUnsaved(`恢复 v${row.version}`, proceed)
  else void proceed()
}

/** 归档要确认（改了服务端状态、会停掉按名字取用的自动化）；取消归档是安全的反向动作，直接做。 */
function toggleArchive(row: DefinitionSummary): void {
  if (row.status === 'archived') {
    void catalog.restoreDefinition(row.workflow_id)
    return
  }
  confirmArchive(row.name, row.version, () => {
    void catalog.archiveDefinition(row.workflow_id)
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

const def = computed(() => documentStore.definition)

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
  documentStore.setMeta({ name: (event.target as HTMLInputElement).value })
}

function onDescription(event: Event): void {
  documentStore.setMeta({ description: (event.target as HTMLTextAreaElement).value })
}
</script>

<template>
  <section v-if="def !== null" class="wf-def">
    <div class="wf-def__library">
      <div class="wf-def__heading">
        <h3 class="wf-def__title">工作流库</h3>
        <label v-if="archivedCount > 0" class="wf-def__filter">
          <input v-model="showArchived" type="checkbox" />
          显示归档（{{ archivedCount }}）
        </label>
      </div>
      <label class="wf-def__search">
        <SearchOutlined aria-hidden="true" />
        <input v-model="query" type="search" class="wf-def__input" aria-label="搜索工作流" placeholder="搜索流程名称或 ID" />
      </label>
      <ul v-if="visibleDefinitions.length" class="wf-def__list">
        <li v-for="row in visibleDefinitions" :key="row.workflow_id" class="wf-def__list-item"
          :class="{ 'is-standard': row.name === STANDARD_CHAIN_NAME }">
          <span class="wf-def__name" :title="row.name">
            <ApartmentOutlined v-if="row.name === STANDARD_CHAIN_NAME" aria-hidden="true" />
            {{ row.name }}
          </span>
          <span class="wf-def__ver">v{{ row.version }}</span>
          <span class="wf-def__muted wf-def__meta">{{ row.node_count }} 节点 / {{ row.edge_count }} 连线</span>
          <span class="wf-def__muted wf-def__state">{{ definitionStatusLabel(row.status) }}</span>
          <button type="button" class="wf-def__button wf-def__button--open" :disabled="documentStore.loading"
            :data-testid="`open-${row.workflow_id}`" @click="open(row)">打开</button>
          <button type="button" class="wf-def__button wf-def__button--archive"
            :disabled="documentStore.loading" :data-testid="`archive-${row.workflow_id}`" @click="toggleArchive(row)">
            {{ row.status === 'archived' ? '取消归档' : '归档' }}
          </button>
        </li>
      </ul>
      <p v-else class="wf-def__empty">
        {{ catalog.definitions.length === 0 ? '尚无已保存定义。' : (query.trim() ? '没有匹配的工作流。' : '当前没有启用的工作流。') }}
      </p>
      <FreshnessBar
        :fetched-at="catalog.definitionsUpdatedAt"
        :label="`服务端定义（${catalog.definitions.length}） · 每 15 秒更新`"
        :interval-ms="15_000"
        stacked
      />
    </div>
    <h3 class="wf-def__title">当前画布</h3>
    <label class="wf-def__row">
      <span>名称</span>
      <input class="wf-def__input" type="text" :value="def.name" :readonly="!documentStore.canEdit" :maxlength="MAX_DEFINITION_NAME_CHARS" @change="onName" />
    </label>
    <label class="wf-def__row">
      <span>描述</span>
      <textarea class="wf-def__input" rows="2" :value="def.description" :readonly="!documentStore.canEdit" :maxlength="MAX_DESCRIPTION_CHARS" @change="onDescription" />
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
            :readonly="!documentStore.canEdit"
            :aria-label="`${nameOf(edge.source)} 到 ${nameOf(edge.target)}的连线条件`"
            placeholder="边条件（分支名）"
            maxlength="64"
            @change="documentStore.updateEdgeCondition(edge.id, ($event.target as HTMLInputElement).value)"
          />
          <button type="button" class="wf-def__button wf-def__button--danger" :disabled="!documentStore.canEdit" @click="documentStore.removeEdge(edge.id)">删除</button>
        </div>
        <p class="wf-def__hint">{{ CONDITION_HINT }}</p>
      </li>
    </ul>

    <h4 class="wf-def__subtitle">本地校验</h4>
    <p class="wf-def__hint">以下仅为提交前拦截，服务端仍会复核类型注册、重名版本与运行期配置。</p>
    <ul v-if="documentStore.validationErrors.length > 0" class="wf-def__errors">
      <li v-for="message in documentStore.validationErrors" :key="message">{{ message }}</li>
    </ul>
    <p v-else class="wf-def__ok">{{ documentStore.autoSaveEnabled ? '校验通过，可发布。' : '校验通过。' }}</p>

    <h4 class="wf-def__subtitle">版本历史（{{ documentStore.versions.length }}）</h4>
    <p v-if="documentStore.versions.length === 0" class="wf-def__empty">
      还没有发布版本；画布草稿会自动保存，点「发布版本」后这里出现 v1。
    </p>
    <ul v-else class="wf-def__versions">
      <li v-for="row in documentStore.versions" :key="row.workflow_id" class="wf-def__version">
        <span class="wf-def__ver">v{{ row.version }}</span>
        <code class="wf-def__version-id">{{ row.workflow_id }}</code>
        <span class="wf-def__muted">{{ row.node_count }} 节点 / {{ row.edge_count }} 连线</span>
        <span v-if="row.is_current" class="wf-def__current">当前版本</span>
        <button
          type="button"
          class="wf-def__button"
          :disabled="documentStore.loading"
          :data-testid="`restore-${row.workflow_id}`"
          @click="restore(row)"
        >
          恢复为草稿
        </button>
      </li>
    </ul>


  </section>
</template>

<style scoped>
.wf-def {
  display: flex;
  flex-direction: column;
  min-width: 0;
  gap: 10px;
  padding: 14px;
  border-top: 1px solid var(--aeg-color-border);
}
.wf-def__library {
  display: grid;
  min-width: 0;
  gap: 8px;
  padding: 0 0 12px;
  border-bottom: 1px solid var(--aeg-color-border);
}
.wf-def__heading {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  align-items: center;
  gap: 4px 8px;
}
.wf-def__filter {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  min-height: 32px;
  color: var(--aeg-color-text-secondary);
  font-size: 12px;
  cursor: pointer;
}
.wf-def__search {
  position: relative;
  display: grid;
  color: var(--aeg-color-text-secondary);
}
.wf-def__search > .anticon {
  position: absolute;
  top: 50%;
  left: 10px;
  transform: translateY(-50%);
  pointer-events: none;
}
.wf-def__search > input {
  min-height: 36px;
  padding-left: 31px;
}
.wf-def__title,
.wf-def__subtitle {
  margin: 0;
  font-size: 14px;
}
.wf-def__subtitle {
  margin-top: 6px;
  color: var(--aeg-color-text-secondary);
  font-size: 12px;
}
.wf-def__row {
  display: flex;
  flex-direction: column;
  gap: 2px;
  color: var(--aeg-color-text-secondary);
  font-size: 12px;
}
.wf-def__input {
  min-width: 0;
  padding: 5px 8px;
  border: 1px solid var(--aeg-color-border-strong);
  border-radius: var(--aeg-radius-sm);
  background: var(--aeg-color-sunken);
  color: var(--aeg-color-text);
  font-size: 12px;
}
.wf-def__identity {
  display: flex;
  gap: 8px;
  margin: 0;
  color: var(--aeg-color-text-secondary);
  font-size: 11px;
}
.wf-def__edges,
.wf-def__list,
.wf-def__versions,
.wf-def__errors {
  margin: 0;
  padding: 0;
  list-style: none;
}
.wf-def__versions {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.wf-def__version {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  gap: 4px 6px;
  align-items: center;
  padding: 5px 8px;
  border: 1px solid var(--aeg-color-border);
  border-radius: 8px;
  font-size: 11px;
}
.wf-def__version-id {
  min-width: 0;
  overflow: hidden;
  color: var(--aeg-color-text-secondary);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.wf-def__version .wf-def__button {
  grid-column: 3;
  grid-row: 1 / span 2;
}
.wf-def__current {
  color: var(--aeg-color-status-success);
  font-weight: 600;
}
.wf-def__errors {
  color: var(--aeg-color-status-danger);
  font-size: 12px;
}
.wf-def__edge {
  padding: 8px;
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-sm);
}
.wf-def__list-item {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;
  column-gap: 6px;
  row-gap: 1px;
  align-items: center;
  padding: 3px 8px;
  border: 1px solid var(--aeg-color-border-subtle);
  border-radius: var(--aeg-radius-md);
  background: var(--aeg-color-overview-tile);
  font-size: 12px;
  transition: background var(--aeg-motion-base) ease, border-color var(--aeg-motion-base) ease;
}
.wf-def__list-item:hover,
.wf-def__list-item.is-standard {
  background: var(--aeg-color-brand-soft);
  border-color: var(--aeg-color-brand-line);
}
.wf-def__list-item.is-standard .anticon {
  margin-right: 2px;
  color: var(--aeg-color-brand-hover);
}
.wf-def__name {
  grid-area: 1 / 1 / 2 / 2;
  min-width: 0;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.wf-def__ver {
  grid-area: 1 / 2 / 2 / 3;
  color: var(--aeg-color-text-secondary);
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
  min-height: 29px;
  padding: 2px 8px;
  border: 1px solid var(--aeg-color-border-strong);
  border-radius: var(--aeg-radius-sm);
  background: var(--aeg-color-card);
  color: var(--aeg-color-text);
  font-size: 12px;
  white-space: nowrap;
  cursor: pointer;
}
.wf-def__button--open {
  border-color: var(--aeg-color-brand-line);
  background: var(--aeg-color-brand-soft);
  color: var(--aeg-color-brand-hover);
}
.wf-def__button--archive {
  min-height: 22px;
  padding-block: 0;
  border-color: transparent;
  background: transparent;
  color: var(--aeg-color-text-secondary);
}
.wf-def__button:not(:disabled):hover {
  border-color: var(--aeg-color-brand-hover);
}
.wf-def__button:disabled {
  cursor: not-allowed;
  opacity: 0.65;
}
.wf-def__button--danger {
  border-color: var(--aeg-color-status-danger);
  color: var(--aeg-color-status-danger);
}
.wf-def__hint,
.wf-def__empty,
.wf-def__ok,
.wf-def__muted {
  margin: 0;
  color: var(--aeg-color-text-secondary);
  font-size: 11px;
}
.wf-def__ok {
  color: var(--aeg-color-status-success);
}
</style>
