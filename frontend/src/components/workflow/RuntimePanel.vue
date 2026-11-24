<script setup lang="ts">
/**
 * 实例级控制：试运行 / 启动 / 中止 / 轮询 / 实例列表。
 * 节点级运行中操作在 RuntimeActions，这里只处理整条链路。
 */
import { computed, nextTick, ref } from 'vue'
import { ApartmentOutlined, ExperimentOutlined, PlayCircleOutlined } from '@ant-design/icons-vue'

import FreshnessBar from '@/components/FreshnessBar.vue'
import type { InstanceDetail } from '@/api/workflow'
import type { RunTarget } from '@/stores/workflow/run'
import { INSTANCE_STATUS_LABELS, isInstanceStatus, type InstanceStatus, type JsonValue } from '@/utils/graph'

import RunInputDialog from './RunInputDialog.vue'
import RunTimeline from './RunTimeline.vue'
import { useEditorContext } from './editor/context'

const emit = defineEmits<{
  /** 时间线某一行的点击：要看那颗节点的参数，所以连带打开节点面板。 */
  'focus-node': [nodeId: string]
  /** 点实例行：只把画布带到它卡住（或第一步）的节点上，右栏得留在运行态——否则刚展开的时间线会被换掉。 */
  'locate-node': [nodeId: string]
}>()

const { document: documentStore, editor, run } = useEditorContext()

const abortReason = ref('')
const onlyWaiting = ref(false)
const dialogMode = ref<'preview' | 'start' | null>(null)
const runTarget = ref<RunTarget | null>(null)
const reportHeading = ref<HTMLHeadingElement | null>(null)
const chainStages = ['感知', '研判', '决策', '执行', '反馈']

/** 实例载荷里只有 node_id，名字得回自己手上这份定义取；取不到就退回 ID（不猜名字）。 */
const nodeNames = computed<Record<string, string>>(() =>
  Object.fromEntries((documentStore.definition?.nodes ?? []).map((node) => [node.node_id, node.name])),
)

const inInstance = computed(() => editor.mode.kind === 'instance')
const draftMode = computed(() => editor.mode.kind === 'draft')
const previewing = computed(() => draftMode.value && run.preview !== null)

const timelineRuns = computed(() => {
  if (inInstance.value) return run.instance?.nodes ?? []
  return run.preview?.nodes ?? []
})

const timelineOutputs = computed(() => (inInstance.value ? null : (run.preview?.results ?? null)))
const activeRun = computed(() => (inInstance.value ? run.instance : (previewing.value ? run.preview : null)))
const previewBlock = computed(() => {
  if (!draftMode.value) return '恢复为草稿后可试运行'
  if (documentStore.lineageId === '') return '先保存或打开一份流程'
  return ''
})

const statusLabel = computed<string>(() => {
  if (previewing.value) {
    const status = run.previewStatus
    return status === null ? '试运行完成' : `试运行 · ${INSTANCE_STATUS_LABELS[status]}`
  }
  const status = run.instanceStatus
  return status === null ? '未选择实例' : INSTANCE_STATUS_LABELS[status]
})

/** 启动跑的是已发布版本，画布上未发布的草稿改动不参与本次执行。 */
const boundVersion = computed<string>(() => {
  if (run.startWorkflowId === null) return '发布后可启动'
  return run.startVersion === null ? '将执行所选已发布版本' : `将执行已发布的 v${run.startVersion}`
})

function label(status: string): string {
  return isInstanceStatus(status) ? INSTANCE_STATUS_LABELS[status] : status
}

function statusTone(status: string): InstanceStatus | 'unknown' {
  return isInstanceStatus(status) ? status : 'unknown'
}

const busy = computed(() => editor.loading)

function closeDialog(): void {
  dialogMode.value = null
  runTarget.value = null
}

async function openFormalRun(): Promise<void> {
  const target = await run.loadRunTarget()
  if (target === null) return
  runTarget.value = target
  dialogMode.value = 'start'
}

async function submitRun(payload: Record<string, JsonValue>): Promise<void> {
  const mode = dialogMode.value
  if (mode === null || (mode === 'start' && runTarget.value === null)) return
  const ok = mode === 'preview' ? await run.startPreview(payload) : await run.startInstance(payload, runTarget.value?.workflowId)
  if (ok) closeDialog()
}

function abort(): void {
  void run.abortInstance(abortReason.value)
  abortReason.value = ''
}

/** 标准链路默认查看全图，待签或失败时优先定位问题节点。 */
async function pick(row: InstanceDetail): Promise<void> {
  if (!(await run.focusInstance(row.instance_id))) return
  const nodes = run.instance?.nodes ?? row.nodes
  const blocked = nodes.find((node) => node.state === 'awaiting_human' || node.state === 'failed')
  const target = blocked ?? (run.systemChain ? undefined : nodes[0])
  if (target !== undefined) emit('locate-node', target.node_id)
  await nextTick()
  reportHeading.value?.scrollIntoView?.({ block: 'nearest' })
}

/**
 * 等人签的实例排在最前，其余按新到旧。
 *
 * 真机量过一轮：低置信度上报开出的核签工单只出现在这张列表里，
 * 而列表是后端 `instances()` 的创建顺序（最旧的在第一行），11 张等签的混在 16 条里
 * 没有任何一处说明"有几张在等人"——`/dashboard`、`/warnings` 上"核签/待签/人工"字样各 0 次。
 * 一张工单等着的是"这条预警要不要按现状生效"，靠人滚动去撞见是不负责任的。
 */
const waitingRows = computed(() => run.instances.filter((row) => row.status === 'waiting'))
const listedInstances = computed(() => {
  const newestFirst = [...run.instances].reverse()
  const ranked = [...newestFirst].sort((a, b) => (b.status === 'waiting' ? 1 : 0) - (a.status === 'waiting' ? 1 : 0))
  return onlyWaiting.value ? ranked.filter((row) => row.status === 'waiting') : ranked
})
</script>

<template>
  <section class="wf-run">
    <div class="wf-run__heading">
      <h3 class="wf-run__title">运行控制</h3>
      <RouterLink class="wf-run__link" to="/simulation">情景推演 ↗</RouterLink>
    </div>
    <div v-if="inInstance" class="wf-run__actions">
      <div class="wf-run__heading">
        <p class="wf-run__status">
          <strong>{{ statusLabel }}</strong>
          <span v-if="run.polling" class="wf-run__polling">实时更新</span>
        </p>
        <button type="button" class="wf-run__button" data-testid="exit-instance" @click="run.exitInstance()">返回草稿</button>
      </div>
      <div v-if="run.instanceMutable" class="wf-run__bar">
        <input v-model="abortReason" class="wf-run__input" type="text" aria-label="中止理由（可选）" placeholder="中止理由（可选）" maxlength="128" />
        <button type="button" class="wf-run__button wf-run__button--danger" :disabled="busy" @click="abort">中止实例</button>
      </div>
    </div>
    <div v-else-if="run.systemChain" class="wf-run__actions wf-run__system">
      <strong><ApartmentOutlined aria-hidden="true" /> 标准五段处置链路</strong>
      <ol class="wf-run__stages">
        <li v-for="stage in chainStages" :key="stage">{{ stage }}</li>
      </ol>
      <p class="wf-run__hint">由监测数据或人工上报触发。演练可在运行实例中查看各阶段结果。</p>
      <RouterLink class="wf-run__button wf-run__button--primary" to="/monitor#drill">前往处置链演练</RouterLink>
    </div>
    <div v-else class="wf-run__actions">
      <div class="wf-run__action">
        <ExperimentOutlined class="wf-run__icon" aria-hidden="true" />
        <div>
          <strong>草稿试运行</strong>
          <p class="wf-run__hint">{{ previewBlock || '使用当前画布 · 模拟外部动作' }}</p>
        </div>
        <button type="button" class="wf-run__button wf-run__button--primary" :disabled="busy || previewBlock !== ''"
          :title="previewBlock || '使用当前草稿试运行'" data-testid="preview-start" @click="dialogMode = 'preview'">试运行</button>
      </div>
      <div class="wf-run__action">
        <PlayCircleOutlined class="wf-run__icon" aria-hidden="true" />
        <div>
          <strong>已发布版本</strong>
          <p class="wf-run__hint">{{ boundVersion }}</p>
        </div>
        <button type="button" class="wf-run__button" :disabled="busy || run.startWorkflowId === null"
          data-testid="instance-start" @click="openFormalRun">正式启动</button>
      </div>
    </div>

    <!-- 试运行的安全横幅：结果旁边必须写清"没有真的发出去"，否则一次演练会被读成一次处置。 -->
    <section v-if="previewing" class="wf-run__preview" data-testid="preview-banner">
      <p class="wf-run__preview-title">{{ statusLabel }}</p>
      <p class="wf-run__preview-note">
        没有产生外部副作用，结果保留 30 分钟。人工核签按第一项模拟通过。
      </p>
      <button type="button" class="wf-run__button" @click="run.clearPreview()">清除试运行结果</button>
    </section>

    <p v-if="run.awaitingNodes.length > 0" class="wf-run__awaiting">
      待人工核签：{{ run.awaitingNodes.map((node) => node.node_id).join('、') }}
    </p>
    <p v-if="run.instance?.error" class="wf-run__error">{{ run.instance.error }}</p>
    <p v-if="previewing && run.preview?.error" class="wf-run__error">试运行失败：{{ run.preview.error }}</p>

    <h4 v-if="activeRun" ref="reportHeading" class="wf-run__subtitle">{{ previewing ? '试运行步骤' : '执行步骤' }}</h4>
    <dl v-if="activeRun" class="wf-run__identity">
      <dt>{{ previewing ? '预览' : '实例' }}</dt><dd><code>{{ activeRun.instance_id }}</code></dd>
      <template v-if="activeRun.trace_id"><dt>链路</dt><dd><code>{{ activeRun.trace_id }}</code></dd></template>
      <template v-if="inInstance"><dt>版本</dt><dd>v{{ activeRun.workflow_version }}</dd></template>
    </dl>
    <RunTimeline
      v-if="activeRun"
      :runs="timelineRuns"
      :names="nodeNames"
      :outputs="timelineOutputs"
      :include-live-output="inInstance"
      @select="emit('focus-node', $event)"
    />
    <h4 class="wf-run__subtitle">
      实例记录（{{ run.instances.length }}）
      <label v-if="waitingRows.length > 0 || onlyWaiting" class="wf-run__waiting">
        等人签 {{ waitingRows.length }} 张
        <input v-model="onlyWaiting" type="checkbox" data-testid="only-waiting" />
        只看这些
      </label>
    </h4>
    <FreshnessBar
      :fetched-at="run.instancesUpdatedAt"
      label="实例记录 · 每 15 秒更新"
      :interval-ms="15_000"
      stacked
    />
    <!-- 勾了"只看这些"之后一张都没剩下时，必须说一句"签完了"：
         留一个空列表不解释，读的人会得出"没有工单功能"的结论（监测页同款坑） -->
    <p v-if="onlyWaiting && waitingRows.length === 0" class="wf-run__hint">这一列里没有等人签的实例了。</p>
    <ul class="wf-run__list">
      <li v-for="row in listedInstances" :key="row.instance_id">
        <button
          type="button"
          class="wf-run__pick"
          :class="[`is-${statusTone(row.status)}`, { 'is-picked': run.instance?.instance_id === row.instance_id }]"
          :aria-pressed="run.instance?.instance_id === row.instance_id"
          @click="pick(row)"
        >
          <span class="wf-run__id">{{ row.instance_id }}</span>
          <span class="wf-run__state">{{ label(row.status) }}</span>
          <span class="wf-run__trace">{{ row.trace_id }}</span>
        </button>
      </li>
    </ul>
    <p v-if="run.instances.length === 0" class="wf-run__hint">尚无实例。正式启动或处置链演练后可在这里查看。</p>

    <Teleport to="body">
      <RunInputDialog
        v-if="dialogMode !== null"
        :mode="dialogMode"
        :busy="busy"
        :target="runTarget"
        @close="closeDialog"
        @submit="submitRun"
      />
    </Teleport>
  </section>
</template>

<style scoped>
.wf-run {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px;
  border-top: 1px solid var(--aeg-color-border);
}
.wf-run__heading,
.wf-run__bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}
.wf-run__title,
.wf-run__subtitle {
  margin: 0;
  font-size: 14px;
}
.wf-run__subtitle {
  margin-top: 4px;
}
.wf-run__link {
  display: inline-flex;
  align-items: center;
  min-height: 36px;
  color: var(--aeg-color-brand-hover);
  font-size: 12px;
}
.wf-run__actions {
  display: grid;
  gap: 4px;
  min-width: 0;
  padding: 10px;
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-lg);
  background: var(--aeg-color-overview-tile);
}
.wf-run__action {
  display: grid;
  grid-template-columns: 30px minmax(0, 1fr) auto;
  gap: 8px;
  align-items: center;
  font-size: 13px;
}
.wf-run__action + .wf-run__action {
  margin-top: 4px;
  padding-top: 8px;
  border-top: 1px solid var(--aeg-color-border-subtle);
}
.wf-run__icon {
  display: grid;
  width: 30px;
  height: 30px;
  place-items: center;
  border-radius: var(--aeg-radius-sm);
  background: var(--aeg-color-brand-soft);
  color: var(--aeg-color-brand-hover);
}
.wf-run__system {
  justify-items: start;
  gap: 8px;
  font-size: 13px;
}
.wf-run__stages {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.wf-run__stages li {
  padding: 2px 6px;
  border: 1px solid var(--aeg-color-brand-line);
  border-radius: var(--aeg-radius-sm);
  color: var(--aeg-color-brand-hover);
  font-size: 12px;
}
.wf-run__waiting {
  margin-left: 6px;
  color: var(--aeg-color-status-danger);
  font-weight: 400;
}
.wf-run__button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 36px;
  padding: 4px 10px;
  border: 1px solid var(--aeg-color-border-strong);
  border-radius: var(--aeg-radius-sm);
  background: var(--aeg-color-card);
  color: var(--aeg-color-text);
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
  cursor: pointer;
}
.wf-run__button:not(:disabled):hover {
  border-color: var(--aeg-color-brand-hover);
}
.wf-run__button--primary {
  border-color: var(--aeg-color-brand);
  background: var(--aeg-color-brand);
  color: var(--aeg-color-text-inverse);
}
.wf-run__button--danger {
  border-color: var(--aeg-color-status-danger);
  color: var(--aeg-color-status-danger);
}
.wf-run__button:disabled {
  border-color: var(--aeg-color-border);
  background: var(--aeg-color-sunken);
  color: var(--aeg-color-text-secondary);
  cursor: not-allowed;
}
.wf-run__input {
  flex: 1 1 120px;
  min-width: 0;
  min-height: 36px;
  padding: 4px 8px;
  border: 1px solid var(--aeg-color-border-strong);
  border-radius: var(--aeg-radius-sm);
  background: var(--aeg-color-sunken);
  color: var(--aeg-color-text);
  font-size: 12px;
}
.wf-run__status {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin: 0;
  font-size: 12px;
}
.wf-run__polling {
  color: var(--aeg-color-status-success);
}
.wf-run__identity {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 4px 10px;
  margin: 0;
  padding: 8px 10px;
  border: 1px solid var(--aeg-color-border-subtle);
  border-radius: var(--aeg-radius-md);
  color: var(--aeg-color-text-secondary);
  font-size: 12px;
}
.wf-run__identity dd {
  margin: 0;
  overflow-wrap: anywhere;
  color: var(--aeg-color-text);
}
.wf-run__preview {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px;
  border: 1px solid var(--aeg-color-brand-line);
  border-radius: var(--aeg-radius-sm);
  background: var(--aeg-color-brand-soft);
}
.wf-run__preview-title {
  margin: 0;
  color: var(--aeg-color-brand-hover);
  font-size: 12px;
  font-weight: 600;
}
.wf-run__preview-note {
  margin: 0;
  color: var(--aeg-color-text-secondary);
  font-size: 11px;
  line-height: 1.6;
}
.wf-run__preview .wf-run__button {
  align-self: flex-start;
}
.wf-run__hint,
.wf-run__trace {
  margin: 0;
  color: var(--aeg-color-text-secondary);
  font-size: 11px;
}
.wf-run__awaiting {
  margin: 0;
  color: var(--aeg-color-status-warning);
  font-size: 12px;
}
.wf-run__error {
  margin: 0;
  color: var(--aeg-color-status-danger);
  font-size: 12px;
}
.wf-run__list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin: 0;
  padding: 0;
  list-style: none;
  max-height: 160px;
  overflow-y: auto;
}
.wf-run__pick {
  display: flex;
  gap: 8px;
  align-items: center;
  width: 100%;
  padding: 7px 10px;
  border: 1px solid transparent;
  border-radius: 10px;
  background: transparent;
  color: var(--aeg-color-text);
  font-size: 11px;
  text-align: left;
  cursor: pointer;
  transition: background var(--aeg-motion-base) ease, border-color var(--aeg-motion-base) ease;
}
.wf-run__pick:hover {
  background: var(--aeg-color-brand-soft);
  border-color: var(--aeg-color-brand-line);
}
.wf-run__pick.is-waiting {
  border-color: var(--aeg-color-status-warning);
}
.wf-run__pick.is-failed {
  border-color: var(--aeg-color-status-danger);
}
.wf-run__pick.is-picked {
  background: var(--aeg-color-brand-soft);
  border-color: var(--aeg-color-brand);
}
/* 一行里两个长 ID 会把"已完成"挤成"已完/成"（真机 @1440 量到 4 处）：
   状态词不许折，两个 ID 改成省略号——完整 ID 在点开的实例详情里看得到。 */
.wf-run__state {
  flex: 0 0 auto;
  white-space: nowrap;
}
.wf-run__id,
.wf-run__trace {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
