<script setup lang="ts">
/**
 * AlertStack —— 助手面板顶部的「在案告警」区。
 *
 * 数据真源是台账（`useAlerts` 里已写明两条来源的分工），这里只负责呈现与处置：
 * - 每张卡：等级徽标（`--aeg-risk-text-*`，与风险瓦片墙同源）、标题/区域/时间、
 *   演练标识（演练必须看得见，不能和真实混排）；
 * - 三种处置：接手（duty）、转派（lead，inline 输入受理席位）、稍后（15 分钟）；
 * - 席位从本组件顶部一行选择（`operatorSeat`），写动作全站共用这一个来源。
 */

import { computed, onMounted, ref } from 'vue'

import { operatorSeat, setOperatorSeat } from '@/api/operatorSeat'
import { RISK_LABELS, type RiskLevel } from '@/api/types'
import { useAlerts } from '@/composables/useAlerts'

const { cards, syncing, syncError, openCards, refresh, ack, transfer, snooze, snoozeActive: isSnoozed, notices, noticeError, dismissNotice } =
  useAlerts()

const seatDraft = ref(operatorSeat.value)
const busyId = ref('')
const message = ref('')

onMounted(() => {
  refresh()
})

const levelLabel = (level: number): string => RISK_LABELS[(level as RiskLevel) in RISK_LABELS ? (level as RiskLevel) : 5]

function timeOf(card: { created_at: string }): string {
  const moment = Date.parse(card.created_at)
  return Number.isFinite(moment) ? new Date(moment).toLocaleTimeString('zh-CN', { hour12: false }) : '时间未取到'
}

function statusLine(card: { status: string; drill: boolean }): string {
  if (card.drill) return '演练'
  if (card.status === 'escalated') return '已升级'
  if (card.status === 'acked') return '已接手'
  return ''
}

async function run(card: (typeof cards.value)[number], action: () => Promise<string>): Promise<void> {
  if (busyId.value !== '') return
  busyId.value = card.alert_id || card.warning_id
  message.value = await action()
  busyId.value = ''
}

const onAck = (card: (typeof cards.value)[number]) => run(card, () => ack(card))
const onSnooze = (card: (typeof cards.value)[number]) => run(card, () => snooze(card, 15))

const transferTarget = ref('')
const transferringId = ref('')

function toggleTransfer(card: (typeof cards.value)[number]): void {
  transferringId.value = transferringId.value === card.alert_id ? '' : card.alert_id
  transferTarget.value = ''
  message.value = ''
}

async function confirmTransfer(card: (typeof cards.value)[number]): Promise<void> {
  const target = transferTarget.value.trim()
  if (target === '') {
    message.value = '先填受理席位的 ID（如 op-lead）'
    return
  }
  await run(card, () => transfer(card, target))
  transferringId.value = ''
}

function saveSeat(): void {
  setOperatorSeat(seatDraft.value)
  message.value = operatorSeat.value === '' ? '已清空席位：写动作将被拒绝' : ''
}

const shown = computed(() => openCards.value)
</script>

<template>
  <section class="alerts" data-testid="alert-stack">
    <header class="alerts__bar">
      <span class="alerts__title">在案告警</span>
      <span v-if="syncing" class="alerts__sync">对账中…</span>
      <span v-else-if="syncError" class="alerts__sync alerts__sync--bad">{{ syncError }}</span>
      <button class="alerts__refresh" type="button" data-testid="alert-refresh" @click="refresh()">对账</button>
    </header>

    <div class="alerts__seat">
      <label class="alerts__seat-label" for="alert-seat-input">当前席位</label>
      <input
        id="alert-seat-input"
        v-model="seatDraft"
        class="alerts__seat-input"
        data-testid="alert-seat"
        placeholder="如 op-duty（页脚同源）"
        @change="saveSeat()"
      />
    </div>

    <p v-if="message" class="alerts__message" data-testid="alert-message">{{ message }}</p>
    <p v-if="noticeError" class="alerts__message" data-testid="notice-error">{{ noticeError }}</p>

    <ul v-if="notices.length" class="alerts__notices" data-testid="notice-list">
      <li v-for="notice in notices" :key="notice.id" class="alerts__notice">
        <span class="alerts__notice-kind">{{ notice.kind === 'group' ? '群发' : '值班长' }}</span>
        <span class="alerts__notice-text">{{ notice.message }}</span>
        <button type="button" class="alerts__notice-close" aria-label="关闭通知" @click="dismissNotice(notice.id)">×</button>
      </li>
    </ul>

    <ul class="alerts__list">
      <li
        v-for="card in shown"
        :key="card.warning_id"
        class="alert"
        :class="{ 'alert--drill': card.drill }"
        data-testid="alert-card"
      >
        <span class="alert__level" :style="{ color: `var(--aeg-risk-text-${card.level})` }">
          {{ levelLabel(card.level) }}{{ card.level }}级
        </span>
        <div class="alert__body">
          <p class="alert__headline">
            <strong>{{ card.title || '标题未取到' }}</strong>
            <span v-if="statusLine(card)" class="alert__flag">{{ statusLine(card) }}</span>
            <span v-if="isSnoozed(card)" class="alert__flag alert__flag--muted">稍后提醒中</span>
          </p>
          <p class="alert__meta">
            {{ card.region_code || '区域未取到' }} ｜ {{ timeOf(card) }}
            <template v-if="card.assigned_to">｜ 处置 {{ card.assigned_to }}</template>
            <template v-if="card.ack_by">｜ 确认 {{ card.ack_by }}</template>
          </p>
          <p v-if="busyId === (card.alert_id || card.warning_id)" class="alert__busy">执行中…</p>

          <div class="alert__actions">
            <button
              v-if="card.status !== 'acked'"
              class="alert__btn alert__btn--primary"
              type="button"
              data-testid="alert-ack"
              :disabled="busyId !== ''"
              @click="onAck(card)"
            >
              接手
            </button>
            <button class="alert__btn" type="button" data-testid="alert-transfer" :disabled="busyId !== ''" @click="toggleTransfer(card)">
              转派
            </button>
            <button class="alert__btn" type="button" data-testid="alert-snooze" :disabled="busyId !== ''" @click="onSnooze(card)">
              稍后15分
            </button>
          </div>
          <div v-if="transferringId === card.alert_id" class="alert__transfer">
            <input v-model="transferTarget" class="alert__transfer-input" placeholder="受理席位 ID" @keyup.enter="confirmTransfer(card)" />
            <button class="alert__btn alert__btn--primary" type="button" @click="confirmTransfer(card)">确认转派</button>
          </div>
        </div>
      </li>
    </ul>
    <p v-if="shown.length === 0" class="alerts__empty">当前没有在案告警。新预警发布后会自动出现在这里。</p>
  </section>
</template>

<style scoped>
.alerts {
  display: flex;
  flex-direction: column;
  gap: var(--aeg-space-s2);
  padding: var(--aeg-space-s3);
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-md);
  background: var(--aeg-color-sunken);
}

.alerts__bar {
  display: flex;
  align-items: center;
  gap: var(--aeg-space-s2);
}
.alerts__title {
  font-size: var(--aeg-font-sm);
  font-weight: var(--aeg-weight-semibold);
  color: var(--aeg-color-text);
}
.alerts__sync {
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-text-muted);
}
.alerts__sync--bad {
  color: var(--aeg-color-status-danger);
}
.alerts__refresh {
  margin-left: auto;
  border: 1px solid var(--aeg-color-border-strong);
  border-radius: var(--aeg-radius-sm);
  background: transparent;
  color: var(--aeg-color-text-secondary);
  font-size: var(--aeg-font-xs);
  padding: 2px 8px;
  cursor: pointer;
}

.alerts__seat {
  display: flex;
  align-items: center;
  gap: var(--aeg-space-s2);
}
.alerts__seat-label {
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-text-secondary);
  white-space: nowrap;
}
.alerts__seat-input {
  flex: 1;
  min-width: 0;
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-sm);
  background: var(--aeg-color-card);
  color: var(--aeg-color-text);
  font-size: var(--aeg-font-xs);
  padding: 3px 8px;
}

.alerts__notices {
  list-style: none;
  margin: 0 0 8px;
  padding: 0;
  display: grid;
  gap: 6px;
}

.alerts__notice {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px 10px;
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-sm);
  background: var(--aeg-color-card);
  color: var(--aeg-color-text);
  font-size: var(--aeg-font-xs);
  line-height: 1.5;
}

.alerts__notice-kind {
  flex: none;
  font-weight: 600;
}

.alerts__notice-text {
  flex: 1;
  min-width: 0;
  word-break: break-word;
}

.alerts__notice-close {
  flex: none;
  border: 0;
  background: transparent;
  cursor: pointer;
  font-size: 16px;
  line-height: 1;
  color: inherit;
  opacity: 0.65;
}

.alerts__message {
  margin: 0;
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-status-danger);
}

.alerts__list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--aeg-space-s2);
}

.alert {
  display: flex;
  gap: var(--aeg-space-s2);
  padding: var(--aeg-space-s2);
  border: 1px solid var(--aeg-color-border);
  border-left-width: 3px;
  border-radius: var(--aeg-radius-sm);
  background: var(--aeg-color-card);
}
.alert--drill {
  border-style: dashed;
}
.alert__level {
  flex: none;
  font-size: var(--aeg-font-xs);
  font-weight: var(--aeg-weight-semibold);
  white-space: nowrap;
  padding-top: 2px;
}
.alert__body {
  flex: 1;
  min-width: 0;
}
.alert__headline {
  margin: 0;
  display: flex;
  align-items: baseline;
  gap: var(--aeg-space-s2);
  font-size: var(--aeg-font-sm);
  color: var(--aeg-color-text);
  overflow-wrap: anywhere;
}
.alert__flag {
  flex: none;
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-status-warning);
}
.alert__flag--muted {
  color: var(--aeg-color-text-muted);
}
.alert__meta {
  margin: 2px 0 0;
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-text-secondary);
}
.alert__busy {
  margin: 2px 0 0;
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-text-muted);
}

.alert__actions {
  display: flex;
  gap: var(--aeg-space-s2);
  margin-top: var(--aeg-space-s2);
}
.alert__btn {
  border: 1px solid var(--aeg-color-border-strong);
  border-radius: var(--aeg-radius-sm);
  background: transparent;
  color: var(--aeg-color-text-secondary);
  font-size: var(--aeg-font-xs);
  padding: 2px 10px;
  cursor: pointer;
}
/* 实底主色 + 反色字：浅底蓝字实测 4.37:1 过不了 11px 的 4.5 门槛（e2e 对比度判据），
   白压浅主色 5.98:1、深字压深主色 5.37:1，两主题都过 */
.alert__btn--primary {
  background: var(--aeg-color-brand);
  border-color: var(--aeg-color-brand);
  color: var(--aeg-color-text-inverse);
}
.alert__btn--primary:hover:not(:disabled) {
  background: var(--aeg-color-brand-hover);
  border-color: var(--aeg-color-brand-hover);
}
.alert__btn:disabled {
  opacity: 0.5;
  cursor: default;
}

.alert__transfer {
  display: flex;
  gap: var(--aeg-space-s2);
  margin-top: var(--aeg-space-s2);
}
.alert__transfer-input {
  flex: 1;
  min-width: 0;
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-sm);
  background: var(--aeg-color-card);
  color: var(--aeg-color-text);
  font-size: var(--aeg-font-xs);
  padding: 3px 8px;
}

.alerts__empty {
  margin: 0;
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-text-muted);
}
</style>
