<script setup lang="ts">
/**
 * HandlingBar —— 助手面板里的「处置空间」条（设计文档 §6.1 / FR-4.1–4.4）。
 *
 * 这里只做呈现与处置，状态与对账都在 `useHandlingSpace`：
 * - 一条预警一个空间，空间里的任务按状态给按钮（待认领→认领，处置中→回执，随时可移交）；
 * - 版本冲突不当成"网络抖动"糊过去：后端 409 明确说"已被他人处置"，并重新对账；
 * - 席位输入与告警卡同源，不在本组件另存一份"我是谁"。
 */

import { computed, onMounted, ref } from 'vue'

import type { HandlingTaskDto } from '@/api/spaces'
import { useHandlingSpace } from '@/composables/useHandlingSpace'

const {
  spaces,
  currentSpaceId,
  detail,
  tasks,
  participants,
  syncing,
  error,
  versionConflict,
  persistent,
  refresh,
  select,
  claim,
  ack,
  transfer,
} = useHandlingSpace()

const busyId = ref('')
const message = ref('')
const transferringId = ref('')
const transferTarget = ref('')

const STATUS_LABELS: Record<string, string> = {
  pending: '待认领',
  claimed: '已认领',
  in_progress: '处置中',
  acked: '已回执',
  closed: '已关闭',
}

const shownTasks = computed(() => tasks.value.slice(0, 8))

function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status
}

function onSelect(event: Event): void {
  const target = event.target as HTMLSelectElement
  message.value = ''
  select(target.value)
}

async function run(task: HandlingTaskDto, action: () => Promise<string>): Promise<void> {
  if (busyId.value !== '') return
  busyId.value = task.task_id
  message.value = await action()
  busyId.value = ''
}

const onClaim = (task: HandlingTaskDto) => run(task, () => claim(task))
const onAck = (task: HandlingTaskDto) => run(task, () => ack(task))

function toggleTransfer(task: HandlingTaskDto): void {
  transferringId.value = transferringId.value === task.task_id ? '' : task.task_id
  transferTarget.value = ''
  message.value = ''
}

async function confirmTransfer(task: HandlingTaskDto): Promise<void> {
  const target = transferTarget.value.trim()
  if (target === '') {
    message.value = '先填受理席位的 ID（如 lead-01）'
    return
  }
  await run(task, () => transfer(task, target))
  transferringId.value = ''
}

onMounted(() => {
  refresh()
})
</script>

<template>
  <section class="handling" data-testid="handling-bar">
    <header class="handling__bar">
      <span class="handling__title">处置空间</span>
      <span v-if="syncing" class="handling__sync" data-testid="space-syncing">对账中…</span>
      <button class="handling__btn" type="button" data-testid="space-refresh" @click="refresh()">对账</button>
    </header>

    <div v-if="spaces.length" class="handling__picker">
      <label for="handling-space-select">当前预警</label>
      <select
        id="handling-space-select"
        class="handling__select"
        :value="currentSpaceId"
        data-testid="space-select"
        @change="onSelect"
      >
        <option v-for="space in spaces" :key="space.space_id" :value="space.space_id">
          {{ space.warning_id }} ｜ 未结 {{ space.tasks_open }}/{{ space.tasks_total }}
        </option>
      </select>
    </div>

    <p v-if="error" class="handling__message" data-testid="space-error">{{ error }}</p>
    <p v-if="versionConflict" class="handling__message handling__message--warn" data-testid="space-conflict">
      {{ versionConflict }}
    </p>
    <p v-if="message" class="handling__message" data-testid="space-message">{{ message }}</p>

    <p v-if="!spaces.length && !syncing" class="handling__empty" data-testid="space-empty">
      还没有处置空间：预警落地后会自动建立一条（一预警一空间）。
    </p>

    <template v-else-if="detail">
      <div class="handling__facts" data-testid="space-facts">
        <span>空间状态 {{ detail.space.status === 'open' ? '在案' : '已关闭' }}</span>
        <span>任务 {{ detail.tasks_total }}</span>
        <span>参与 {{ participants.length ? participants.join('、') : '尚无人认领' }}</span>
        <span v-if="!persistent" class="handling__flag">内存形态，重启即失</span>
      </div>

      <ul class="handling__tasks" data-testid="space-tasks">
        <li v-for="task in shownTasks" :key="task.task_id" class="handling__task" data-testid="space-task">
          <span class="handling__status">{{ statusLabel(task.status) }}</span>
          <span class="handling__id">{{ task.stu_id }}</span>
          <span class="handling__meta">v{{ task.version }} ｜ {{ task.assignee || '未指派' }}</span>
          <span class="handling__actions">
            <button
              v-if="task.status === 'pending'"
              class="handling__btn"
              type="button"
              data-testid="task-claim"
              :disabled="busyId !== ''"
              @click="onClaim(task)"
            >
              认领
            </button>
            <button
              v-if="task.status === 'claimed' || task.status === 'in_progress'"
              class="handling__btn"
              type="button"
              data-testid="task-ack"
              :disabled="busyId !== ''"
              @click="onAck(task)"
            >
              回执
            </button>
            <button
              v-if="task.status !== 'closed'"
              class="handling__btn"
              type="button"
              data-testid="task-transfer"
              :disabled="busyId !== ''"
              @click="toggleTransfer(task)"
            >
              移交
            </button>
          </span>
          <div v-if="transferringId === task.task_id" class="handling__transfer">
            <input
              v-model="transferTarget"
              class="handling__input"
              data-testid="task-transfer-target"
              placeholder="受理席位 ID"
            />
            <button class="handling__btn" type="button" data-testid="task-transfer-confirm" @click="confirmTransfer(task)">
              确认移交
            </button>
          </div>
          <span v-if="busyId === task.task_id" class="handling__busy">执行中…</span>
        </li>
      </ul>
      <p v-if="tasks.length > shownTasks.length" class="handling__more">
        还有 {{ tasks.length - shownTasks.length }} 条任务未展开
      </p>
    </template>
  </section>
</template>

<style scoped>
.handling {
  display: flex;
  flex-direction: column;
  gap: var(--aeg-space-s2);
  padding: var(--aeg-space-s3);
  background: var(--aeg-color-card);
  border-bottom: 1px solid var(--aeg-color-border);
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-text-secondary);
}
.handling__bar {
  display: flex;
  align-items: center;
  gap: var(--aeg-space-s3);
}
.handling__title {
  font-size: var(--aeg-font-sm);
  color: var(--aeg-color-text);
  font-weight: 600;
}
.handling__sync {
  color: var(--aeg-color-text-muted);
}
.handling__picker {
  display: flex;
  align-items: center;
  gap: var(--aeg-space-s2);
}
.handling__select,
.handling__input {
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-md);
  background: var(--aeg-color-alter);
  color: var(--aeg-color-text);
  padding: var(--aeg-space-s1) var(--aeg-space-s2);
  font-size: var(--aeg-font-xs);
}
.handling__select {
  flex: 1;
  min-width: 0;
}
.handling__message {
  color: var(--aeg-color-status-danger);
}
.handling__message--warn {
  color: var(--aeg-color-status-warning);
}
.handling__empty {
  color: var(--aeg-color-text-muted);
}
.handling__facts {
  display: flex;
  flex-wrap: wrap;
  gap: var(--aeg-space-s3);
}
.handling__flag {
  color: var(--aeg-color-status-warning);
}
.handling__tasks {
  display: flex;
  flex-direction: column;
  gap: var(--aeg-space-s2);
  max-height: 180px;
  overflow-y: auto;
  list-style: none;
  margin: 0;
  padding: 0;
}
.handling__task {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--aeg-space-s2);
  padding: var(--aeg-space-s2);
  background: var(--aeg-color-alter);
  border-radius: var(--aeg-radius-md);
}
.handling__status {
  min-width: 52px;
  color: var(--aeg-color-text);
  font-weight: 600;
}
.handling__id {
  flex: 1;
  min-width: 90px;
  color: var(--aeg-color-text);
}
.handling__meta {
  color: var(--aeg-color-text-muted);
}
.handling__actions {
  display: flex;
  gap: var(--aeg-space-s2);
}
.handling__btn {
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-md);
  background: var(--aeg-color-card);
  color: var(--aeg-color-text);
  padding: var(--aeg-space-s1) var(--aeg-space-s2);
  font-size: var(--aeg-font-xs);
  cursor: pointer;
}
.handling__btn:disabled {
  color: var(--aeg-color-text-muted);
  cursor: default;
}
.handling__transfer {
  display: flex;
  gap: var(--aeg-space-s2);
  width: 100%;
}
.handling__busy,
.handling__more {
  color: var(--aeg-color-text-muted);
}
</style>
