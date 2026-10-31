<script setup lang="ts">
/**
 * 实例级控制：试运行 / 启动 / 中止 / 轮询 / 实例列表。
 * 节点级运行中操作在 RuntimeActions，这里只处理整条链路。
 */
import { computed, ref } from 'vue'

import FreshnessBar from '@/components/FreshnessBar.vue'
import type { InstanceDetail } from '@/api/workflow'
import { INSTANCE_STATUS_LABELS, isInstanceStatus, type InstanceStatus, type JsonValue } from '@/utils/graph'

import RunInputDialog from './RunInputDialog.vue'
import RunTimeline from './RunTimeline.vue'
import { useEditorContext } from './editor/context'

const emit = defineEmits<{ 'focus-node': [nodeId: string] }>()

const { document: documentStore, editor, run } = useEditorContext()

const abortReason = ref('')
const onlyWaiting = ref(false)
const dialogMode = ref<'preview' | 'start' | null>(null)

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

/** 只有试运行展示逐节点产出：正式实例的载荷可能很大，时间线保持可扫读。 */
const timelineOutputs = computed(() => (inInstance.value ? null : (run.preview?.results ?? null)))

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
  const version = documentStore.publishedVersion
  return version === null ? '尚未发布版本，无法正式启动' : `将执行已发布的 v${version}`
})

function label(status: string): string {
  return isInstanceStatus(status) ? INSTANCE_STATUS_LABELS[status] : status
}

function statusTone(status: string): InstanceStatus | 'unknown' {
  return isInstanceStatus(status) ? status : 'unknown'
}

const busy = computed(() => editor.loading)

async function submitRun(payload: Record<string, JsonValue>): Promise<void> {
  const mode = dialogMode.value
  if (mode === null) return
  const ok = mode === 'preview' ? await run.startPreview(payload) : await run.startInstance(payload)
  if (ok) dialogMode.value = null
}

function abort(): void {
  void run.abortInstance(abortReason.value)
  abortReason.value = ''
}

/** 点实例行：换成那条实例的图，并把画布带到它卡住（或第一步）的节点上。 */
async function pick(row: InstanceDetail): Promise<void> {
  if (!(await run.focusInstance(row.instance_id))) return
  const target = row.nodes.find((node) => node.state === 'awaiting_human') ?? row.nodes[0]
  if (target !== undefined) emit('focus-node', target.node_id)
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
    <h3 class="wf-run__title">运行实例</h3>
    <div class="wf-run__bar">
      <button
        type="button"
        class="wf-run__button wf-run__button--primary"
        :disabled="!draftMode || documentStore.lineageId === ''"
        :title="
          !draftMode
            ? '先返回草稿再试运行'
            : documentStore.lineageId === ''
              ? '空白画布还没有 lineage：先保存草稿'
              : '用当前画布草稿跑一遍，不产生外部副作用'
        "
        data-testid="preview-start"
        @click="dialogMode = 'preview'"
      >
        试运行
      </button>
      <button
        type="button"
        class="wf-run__button"
        :disabled="documentStore.publishedWorkflowId === null || inInstance"
        data-testid="instance-start"
        @click="dialogMode = 'start'"
      >
        正式启动
      </button>
      <button v-if="inInstance" type="button" class="wf-run__button" data-testid="exit-instance" @click="run.exitInstance()">
        返回草稿
      </button>
      <button type="button" class="wf-run__button wf-run__button--danger" :disabled="!run.instanceMutable" @click="abort">
        中止
      </button>
      <input v-model="abortReason" class="wf-run__input" type="text" placeholder="中止理由（可选）" maxlength="128" />
    </div>
    <p class="wf-run__state">
      <span>{{ statusLabel }}</span>
      <span v-if="run.polling" class="wf-run__polling">轮询中</span>
      <span v-else-if="inInstance && run.instance !== null" class="wf-run__paused">已停止轮询</span>
      <code v-if="inInstance && run.instance !== null">{{ run.instance.instance_id }}</code>
      <code v-else-if="run.preview?.preview_id">{{ run.preview.preview_id }}</code>
    </p>
    <p v-if="!previewing" class="wf-run__hint">{{ boundVersion }}</p>

    <!-- 试运行的安全横幅：结果旁边必须写清"没有真的发出去"，否则一次演练会被读成一次处置。 -->
    <section v-if="previewing" class="wf-run__preview" data-testid="preview-banner">
      <p class="wf-run__preview-title">试运行结果：没有写入任何持久面，也没有产生外部副作用</p>
      <p class="wf-run__preview-note">
        通知 / 外呼 / 设备控制 / 预警发布都没有实际执行；人工核签按第一项模拟通过。结果只保留 30 分钟。
      </p>
      <button type="button" class="wf-run__button" @click="run.clearPreview()">清除试运行结果</button>
    </section>

    <p v-if="run.awaitingNodes.length > 0" class="wf-run__awaiting">
      待人工核签：{{ run.awaitingNodes.map((node) => node.node_id).join('、') }}
    </p>
    <p v-if="run.instance?.error" class="wf-run__error">{{ run.instance.error }}</p>
    <p v-if="previewing && run.preview?.error" class="wf-run__error">试运行失败：{{ run.preview.error }}</p>

    <h4 class="wf-run__subtitle">
      服务端实例（{{ run.instances.length }}）
      <label v-if="waitingRows.length > 0 || onlyWaiting" class="wf-run__waiting">
        等人签 {{ waitingRows.length }} 张
        <input v-model="onlyWaiting" type="checkbox" data-testid="only-waiting" />
        只看这些
      </label>
    </h4>
    <FreshnessBar
      :fetched-at="run.instancesUpdatedAt"
      :label="`服务端实例（${run.instances.length}） ｜ 每 15 秒自己对一遍`"
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
          @click="pick(row)"
        >
          <span class="wf-run__id">{{ row.instance_id }}</span>
          <span class="wf-run__state">{{ label(row.status) }}</span>
          <span class="wf-run__trace">{{ row.trace_id }}</span>
        </button>
      </li>
    </ul>
    <p v-if="run.instances.length === 0" class="wf-run__hint">尚无实例。</p>
    <h4 class="wf-run__subtitle">{{ previewing ? '试运行步骤' : '执行步骤' }}</h4>
    <RunTimeline
      :runs="timelineRuns"
      :names="nodeNames"
      :outputs="timelineOutputs"
      @select="emit('focus-node', $event)"
    />

    <Teleport to="body">
      <RunInputDialog
        v-if="dialogMode !== null"
        :mode="dialogMode"
        :busy="busy"
        @close="dialogMode = null"
        @submit="submitRun"
      />
    </Teleport>
  </section>
</template>

<style scoped>
.wf-run {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 12px;
  border-top: 1px solid #f0f0f0;
}
.wf-run__title,
.wf-run__subtitle {
  margin: 0;
  font-size: 13px;
}
.wf-run__subtitle {
  margin-top: 6px;
  color: var(--aeg-color-text-secondary);
  font-size: 12px;
}
.wf-run__waiting {
  margin-left: 6px;
  color: var(--aeg-color-status-danger);
  font-weight: 400;
}
.wf-run__bar {
  display: flex;
  gap: 6px;
}
.wf-run__button {
  padding: 2px 10px;
  border: 1px solid var(--aeg-color-border-strong);
  border-radius: var(--aeg-radius-sm);
  background: var(--aeg-color-card);
  font-size: 12px;
  cursor: pointer;
}
.wf-run__button--primary {
  /* 主色走令牌：浅色 #2563EB 压 text-inverse 是 5.17:1；
     深色主题的主色是提亮过的 #3B82F6，那时 text-inverse 是深蓝黑（5.09:1）——
     写死 #fff 会在深色下掉到 3.68:1（真机 @1440 量的）。 */
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
  flex: 1;
  min-width: 0;
  padding: 2px 6px;
  border: 1px solid var(--aeg-color-border-strong);
  border-radius: var(--aeg-radius-sm);
  font-size: 12px;
}
.wf-run__state {
  display: flex;
  gap: 8px;
  align-items: center;
  margin: 0;
  font-size: 12px;
}
.wf-run__polling {
  color: #13c2c2;
}
.wf-run__paused {
  color: var(--aeg-color-text-secondary);
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
  color: #722ed1;
  font-size: 12px;
}
.wf-run__error {
  margin: 0;
  color: #cf1322;
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
  /* 列表行习语（对标 NexusMind .run-item）：平时透明，悬停上主色雾，
     选中用**边框**而不是底色——一排白底小按钮读起来像表，不像可选的行。 */
  display: flex;
  gap: 8px;
  align-items: center;
  width: 100%;
  padding: 7px 10px;
  border: 1px solid transparent;
  border-radius: 10px;
  background: transparent;
  font-size: 11px;
  text-align: left;
  cursor: pointer;
  transition: background var(--aeg-motion-base) ease, border-color var(--aeg-motion-base) ease;
}
.wf-run__pick:hover {
  background: rgba(37, 99, 235, 0.06);
  border-color: rgba(37, 99, 235, 0.2);
}
.wf-run__pick.is-waiting {
  border-color: #d3adf7;
}
.wf-run__pick.is-failed {
  border-color: #ffa39e;
}
/* 选中态排在状态色之后：同一行既可以"等人签"也可以"我正在看它"，
   边框得说后一句（状态另有左侧那颗标签文字在说）。 */
.wf-run__pick.is-picked {
  background: rgba(37, 99, 235, 0.09);
  border-color: rgba(37, 99, 235, 0.45);
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
