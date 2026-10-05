<script setup lang="ts">
/**
 * 实例级控制：启动 / 中止 / 轮询 / 实例列表。
 * 节点级运行中操作在 RuntimeActions，这里只处理整条链路。
 */
import { computed, ref } from 'vue'

import { useWorkflowStore } from '@/stores/workflow'
import FreshnessBar from '@/components/FreshnessBar.vue'
import { INSTANCE_STATUS_LABELS, isInstanceStatus, type InstanceStatus } from '@/utils/graph'

import RunTimeline from './RunTimeline.vue'

const store = useWorkflowStore()

const abortReason = ref('')
const onlyWaiting = ref(false)

/** 实例载荷里只有 node_id，名字得回自己手上这份定义取；取不到就退回 ID（不猜名字）。 */
const nodeNames = computed<Record<string, string>>(() =>
  Object.fromEntries((store.current?.nodes ?? []).map((node) => [node.node_id, node.name])),
)
const timelineRuns = computed(() => store.instance?.nodes ?? [])

const statusLabel = computed<string>(() => {
  const status = store.instanceStatus
  return status === null ? '未选择实例' : INSTANCE_STATUS_LABELS[status]
})

/** 界面提示：启动跑的是已保存版本，画布上未保存的改动不参与本次执行。 */
const boundVersion = computed<string>(() => {
  const def = store.current
  if (def === null) return ''
  return def.workflow_id === '' ? '定义尚未保存，无法启动' : `将执行已保存的 v${def.version}`
})

function label(status: string): string {
  return isInstanceStatus(status) ? INSTANCE_STATUS_LABELS[status] : status
}

function statusTone(status: string): InstanceStatus | 'unknown' {
  return isInstanceStatus(status) ? status : 'unknown'
}

function start(): void {
  void store.startInstance()
}

function abort(): void {
  void store.abortInstance(abortReason.value)
  abortReason.value = ''
}

/**
 * 等人签的实例排在最前，其余按新到旧。
 *
 * 真机量过一轮：低置信度上报开出的核签工单只出现在这张列表里，
 * 而列表是后端 `instances()` 的创建顺序（最旧的在第一行），11 张等签的混在 16 条里
 * 没有任何一处说明"有几张在等人"——`/dashboard`、`/warnings` 上"核签/待签/人工"字样各 0 次。
 * 一张工单等着的是"这条预警要不要按现状生效"，靠人滚动去撞见是不负责任的。
 */
const waitingRows = computed(() => store.instances.filter((row) => row.status === 'waiting'))
const listedInstances = computed(() => {
  const newestFirst = [...store.instances].reverse()
  const ranked = [...newestFirst].sort((a, b) => (b.status === 'waiting' ? 1 : 0) - (a.status === 'waiting' ? 1 : 0))
  return onlyWaiting.value ? ranked.filter((row) => row.status === 'waiting') : ranked
})
</script>

<template>
  <section class="wf-run">
    <h3 class="wf-run__title">运行实例</h3>
    <div class="wf-run__bar">
      <button type="button" class="wf-run__button wf-run__button--primary" :disabled="!store.canSave || store.current?.workflow_id === ''" @click="start">
        启动实例
      </button>
      <button type="button" class="wf-run__button wf-run__button--danger" :disabled="!store.instanceMutable" @click="abort">
        中止
      </button>
      <input v-model="abortReason" class="wf-run__input" type="text" placeholder="中止理由（可选）" maxlength="128" />
    </div>
    <p class="wf-run__state">
      <span>{{ statusLabel }}</span>
      <span v-if="store.polling" class="wf-run__polling">轮询中</span>
      <span v-else-if="store.instance !== null" class="wf-run__paused">已停止轮询</span>
      <code v-if="store.instance !== null">{{ store.instance.instance_id }}</code>
    </p>
    <p class="wf-run__hint">{{ boundVersion }}</p>
    <p v-if="store.awaitingNodes.length > 0" class="wf-run__awaiting">
      待人工核签：{{ store.awaitingNodes.map((run) => run.node_id).join('、') }}
    </p>
    <p v-if="store.instance?.error" class="wf-run__error">{{ store.instance.error }}</p>

    <h4 class="wf-run__subtitle">
      服务端实例（{{ store.instances.length }}）
      <label v-if="waitingRows.length > 0 || onlyWaiting" class="wf-run__waiting">
        等人签 {{ waitingRows.length }} 张
        <input v-model="onlyWaiting" type="checkbox" data-testid="only-waiting" />
        只看这些
      </label>
    </h4>
    <FreshnessBar
      :fetched-at="store.instancesUpdatedAt"
      :label="`服务端实例（${store.instances.length}） ｜ 每 15 秒自己对一遍`"
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
          :class="[`is-${statusTone(row.status)}`, { 'is-picked': store.instance?.instance_id === row.instance_id }]"
          @click="store.focusInstance(row.instance_id)"
        >
          <span class="wf-run__id">{{ row.instance_id }}</span>
          <span class="wf-run__state">{{ label(row.status) }}</span>
          <span class="wf-run__trace">{{ row.trace_id }}</span>
        </button>
      </li>
    </ul>
    <p v-if="store.instances.length === 0" class="wf-run__hint">尚无实例。</p>
    <h4 class="wf-run__subtitle">执行步骤</h4>
    <RunTimeline :runs="timelineRuns" :names="nodeNames" />
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
  color: #595959;
  font-size: 12px;
}
.wf-run__waiting {
  margin-left: 6px;
  color: #cf1322;
  font-weight: 400;
}
.wf-run__bar {
  display: flex;
  gap: 6px;
}
.wf-run__button {
  padding: 2px 10px;
  border: 1px solid #d9d9d9;
  border-radius: 4px;
  background: #fff;
  font-size: 12px;
  cursor: pointer;
}
.wf-run__button--primary {
  /* 与全站主色 #2563eb 归一（原来是 antd 默认蓝 #1677ff，同一屏两种蓝）；
     白字压 #2563eb 是 5.17:1，压 #1677ff 只有 4.6:1。 */
  border-color: #2563eb;
  background: #2563eb;
  color: #fff;
}
.wf-run__button--danger {
  border-color: #ffa39e;
  color: #cf1322;
}
.wf-run__button:disabled {
  border-color: #f0f0f0;
  background: #f5f5f5;
  color: #5a6072;
  cursor: not-allowed;
}
.wf-run__input {
  flex: 1;
  min-width: 0;
  padding: 2px 6px;
  border: 1px solid #d9d9d9;
  border-radius: 4px;
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
  color: #5a6072;
}
.wf-run__hint,
.wf-run__trace {
  margin: 0;
  color: #5a6072;
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
  transition: background 0.15s ease, border-color 0.15s ease;
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
