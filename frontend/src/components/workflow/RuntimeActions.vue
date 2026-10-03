<script setup lang="ts">
/**
 * 选中节点的运行中操作：改参 / 绕过 / 人工核签。
 * 按钮可用性直接来自引擎规则（engine.py:515、539、582），前端不另立标准也不假装能改。
 */
import { computed, ref } from 'vue'

import { useWorkflowStore } from '@/stores/workflow'
import { nextAvailableId, stateStyle, type NodeDef, type NodeType } from '@/utils/graph'

import { createNodeDef, NODE_META, PALETTE_GROUPS } from './registry'

const props = defineProps<{ node: NodeDef }>()

const store = useWorkflowStore()

const run = computed(() => store.nodeRun(props.node.node_id))
const style = computed(() => stateStyle(run.value?.state ?? 'pending'))
const hasInstance = computed(() => store.instance !== null)
/** 决策候选来自定义里的 options；为空表示引擎未登记候选（如失败转人工），退化为自由文本。 */
const options = computed(() => store.decisionOptions(props.node.node_id))
const comment = ref('')
const reason = ref('')
const customChoice = ref('')
const insertType = ref<NodeType>('notify')

/**
 * 决策按钮的中文标签：候选值由定义里的 options 决定（templates.py:309 是 approve/adjust/reject），
 * 不在表里的原样显示。此前只翻译 approve/reject，adjust 直接露出英文裸值，
 * 而按钮按数组顺序排——中间那颗恰好永远是没人看得懂的 `adjust`。
 */
const DECISION_LABELS: Record<string, string> = {
  approve: '核签通过',
  adjust: '核签更正',
  reject: '核签退回',
}

function optionLabel(option: string): string {
  const label = DECISION_LABELS[option]
  return label === undefined ? option : `${label}（${option}）`
}

function submit(choice: string): void {
  void store.submitDecision(props.node.node_id, choice, comment.value)
  comment.value = ''
}

function bypass(): void {
  void store.bypassNode(props.node.node_id, reason.value)
  reason.value = ''
}

function pushConfig(): void {
  void store.patchRuntimeConfig(props.node.node_id, props.node.config)
}

/** 在选中节点之后插入：后端会把选中节点的原出边重接到新节点（engine.py:558-565）。 */
function insertAfter(): void {
  const taken = (store.instance?.nodes ?? []).map((item) => item.node_id)
  const created = createNodeDef(insertType.value, nextAvailableId(taken, insertType.value))
  void store.insertRuntimeNode(props.node.node_id, created)
}
</script>

<template>
  <section v-if="!hasInstance" class="wf-runtime">
    <p class="wf-runtime__hint">尚未选择运行实例，启动或挑选实例后可在此执行运行中操作。</p>
  </section>
  <section v-else class="wf-runtime">
    <header class="wf-runtime__head">
      <span>运行态</span>
      <em class="wf-runtime__state" :style="{ color: style.color }">{{ run === null ? '无该节点记录' : style.label }}</em>
    </header>
    <p v-if="run !== null" class="wf-runtime__meta">
      已执行 {{ run.attempts }} 次 · 调度时延 {{ Math.round(run.schedule_latency_ms ?? 0) }} ms · 耗时
      {{ Math.round(run.duration_ms ?? 0) }} ms
    </p>
    <p v-if="run?.error" class="wf-runtime__error">{{ run.error }}</p>
    <ul v-if="run !== null && run.notes.length > 0" class="wf-runtime__notes">
      <li v-for="(note, index) in run.notes.slice(-3)" :key="index">{{ note }}</li>
    </ul>

    <div class="wf-runtime__row">
      <button type="button" class="wf-runtime__button" :disabled="!store.canPatchRuntime(node.node_id)" @click="pushConfig">
        以当前参数下发改参
      </button>
      <span class="wf-runtime__hint">仅未开始执行的节点可改参（engine.py:539-540）。</span>
    </div>

    <div class="wf-runtime__row">
      <input v-model="reason" class="wf-runtime__input" type="text" placeholder="旁路理由（可选）" maxlength="128" />
      <button type="button" class="wf-runtime__button" :disabled="!store.canBypassNode(node.node_id)" @click="bypass">
        绕过该节点
      </button>
    </div>
    <span class="wf-runtime__hint">仅待执行/等待中的节点可旁路，已成功的不可（engine.py:582-583）。</span>

    <div class="wf-runtime__row">
      <select v-model="insertType" class="wf-runtime__input">
        <optgroup v-for="group in PALETTE_GROUPS" :key="group.category" :label="group.label">
          <option v-for="type in group.types" :key="type" :value="type">{{ NODE_META[type].label }}</option>
        </optgroup>
      </select>
      <button type="button" class="wf-runtime__button" :disabled="!store.instanceMutable" @click="insertAfter">
        在此节点后插入
      </button>
    </div>
    <span class="wf-runtime__hint">
      插入会重接本节点原出边；引擎不会在插入瞬间驱动新节点（engine.py:547-569），故仅在实例未终态时开放。
    </span>

    <div class="wf-runtime__row">
      <input v-model="comment" class="wf-runtime__input" type="text" placeholder="核签意见（可选）" maxlength="512" />
    </div>
    <div v-if="options.length > 0" class="wf-runtime__decisions">
      <button
        v-for="option in options"
        :key="option"
        type="button"
        class="wf-runtime__button"
        :disabled="!store.canDecideNode(node.node_id)"
        @click="submit(option)"
      >
        {{ optionLabel(option) }}
      </button>
    </div>
    <div v-else class="wf-runtime__row">
      <input v-model="customChoice" class="wf-runtime__input" type="text" placeholder="决策值" />
      <button
        type="button"
        class="wf-runtime__button"
        :disabled="!store.canDecideNode(node.node_id) || customChoice === ''"
        @click="submit(customChoice)"
      >
        提交决策
      </button>
    </div>
    <span class="wf-runtime__hint">仅"待人工核签"状态的节点可提交决策（engine.py:515-516）。</span>
  </section>
</template>

<style scoped>
.wf-runtime {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px;
  border: 1px solid #f0f0f0;
  border-radius: 4px;
}
.wf-runtime__head {
  display: flex;
  justify-content: space-between;
  color: #595959;
  font-size: 12px;
}
.wf-runtime__state {
  font-style: normal;
  font-weight: 600;
}
.wf-runtime__meta,
.wf-runtime__error,
.wf-runtime__notes {
  margin: 0;
  color: #8c8c8c;
  font-size: 11px;
}
.wf-runtime__error {
  color: #cf1322;
}
.wf-runtime__notes {
  padding-left: 16px;
}
.wf-runtime__row {
  display: flex;
  gap: 6px;
  align-items: center;
}
.wf-runtime__decisions {
  display: flex;
  gap: 6px;
}
.wf-runtime__input {
  flex: 1;
  min-width: 0;
  padding: 2px 6px;
  border: 1px solid #d9d9d9;
  border-radius: 4px;
  font-size: 12px;
}
.wf-runtime__button {
  padding: 2px 8px;
  border: 1px solid #d9d9d9;
  border-radius: 4px;
  background: #fff;
  font-size: 12px;
  cursor: pointer;
}
.wf-runtime__button:disabled {
  color: #bfbfbf;
  cursor: not-allowed;
}
.wf-runtime__hint {
  color: #8c8c8c;
  font-size: 11px;
}
</style>
