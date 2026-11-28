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

import { computed, onMounted, ref, useId, watch } from 'vue'

import { clearOperatorSeat, lookupOperatorSeat, operatorSeat, setOperatorSeat } from '@/api/operatorSeat'
import { RISK_LABELS, type RiskLevel } from '@/api/types'
import { useAlerts } from '@/composables/useAlerts'

const pendingOnly = defineModel<boolean>('pendingOnly', { default: false })
const { cards, syncing, syncError, openCards, urgentCards, refresh, ack, transfer, snooze, snoozeActive: isSnoozed, notices, noticeError, dismissNotice } =
  useAlerts()

const seatDraft = ref(operatorSeat.value)
const seatInputId = useId()
const seatInput = ref<HTMLInputElement | null>(null)
const seatProfile = ref<Awaited<ReturnType<typeof lookupOperatorSeat>> | null>(null)
const seatError = ref('')
const checkingSeat = ref(false)
const busyId = ref('')
const message = ref('')
let seatCheck = 0

const seatRoles = { duty: '值班员', lead: '值班长', admin: '管理员' }
const confirmedSeat = computed(() => {
  const profile = seatProfile.value
  if (!profile?.active || profile.operator_id !== operatorSeat.value || !profile.role) return ''
  return `${profile.display_name || profile.operator_id} · ${seatRoles[profile.role]}`
})

onMounted(() => {
  refresh()
  void checkSeat(operatorSeat.value)
})

watch(operatorSeat, (seat) => {
  seatDraft.value = seat
  message.value = ''
  if (seatProfile.value?.operator_id !== seat) {
    seatProfile.value = null
    void checkSeat(seat)
  }
})

const levelLabel = (level: number): string => RISK_LABELS[(level as RiskLevel) in RISK_LABELS ? (level as RiskLevel) : 5]

function timeOf(card: { created_at: string }): string {
  const moment = Date.parse(card.created_at)
  return Number.isFinite(moment) ? new Date(moment).toLocaleTimeString('zh-CN', { hour12: false }) : '时间未取到'
}

function canAct(): boolean {
  if (checkingSeat.value) return false
  if (confirmedSeat.value && seatDraft.value.trim() === operatorSeat.value) return true
  if (!seatError.value) seatError.value = '请在这里确认值班席位后再处置。'
  seatInput.value?.focus()
  return false
}

async function run(card: (typeof cards.value)[number], action: () => Promise<string>): Promise<void> {
  if (busyId.value !== '' || !canAct()) return
  busyId.value = card.alert_id || card.warning_id
  message.value = await action()
  busyId.value = ''
}

const onAck = (card: (typeof cards.value)[number]) => run(card, () => ack(card))
const onSnooze = (card: (typeof cards.value)[number]) => run(card, () => snooze(card, 15))

const transferTarget = ref('')
const transferringId = ref('')

function toggleTransfer(card: (typeof cards.value)[number]): void {
  if (!canAct()) return
  transferringId.value = transferringId.value === card.alert_id ? '' : card.alert_id
  transferTarget.value = ''
  message.value = ''
}

async function confirmTransfer(card: (typeof cards.value)[number]): Promise<void> {
  const target = transferTarget.value.trim()
  if (target === '') {
    message.value = '请输入受理席位编号。'
    return
  }
  await run(card, () => transfer(card, target))
  transferringId.value = ''
}

async function checkSeat(seat: string, save = false): Promise<void> {
  if (save && !/^[a-zA-Z0-9_-]{4,32}$/.test(seat)) {
    seatError.value = seat ? '席位编号为 4–32 位字母、数字、连字符或下划线。' : '请输入已配置的值班席位编号。'
    seatInput.value?.focus()
    return
  }
  const check = ++seatCheck
  checkingSeat.value = true
  seatError.value = ''
  try {
    const profile = await lookupOperatorSeat(seat)
    if (check !== seatCheck) return
    if (seat && (profile.operator_id !== seat || !profile.active || !profile.role)) {
      if (seat === operatorSeat.value) seatProfile.value = null
      seatError.value = '这个席位不存在或已停用，请核对席位编号。'
      return
    }
    seatProfile.value = profile
    if (save) {
      setOperatorSeat(seat)
      seatDraft.value = seat
      refresh()
    }
  } catch (error) {
    if (check === seatCheck) seatError.value = error instanceof Error ? error.message : '暂时无法验证值班席位。'
  } finally {
    if (check === seatCheck) checkingSeat.value = false
  }
}

function leaveSeat(): void {
  clearOperatorSeat()
  refresh()
}

const listTitle = computed(() => pendingOnly.value ? '待确认告警' : '在案告警')
const shown = computed(() => pendingOnly.value ? urgentCards.value : openCards.value)
</script>

<template>
  <section class="alerts" :aria-label="listTitle" tabindex="-1" data-testid="alert-stack">
    <header class="alerts__bar">
      <span class="alerts__title">{{ listTitle }}</span>
      <span v-if="shown.length" class="alerts__count" :aria-label="`${shown.length} 条${listTitle}`">{{ shown.length }}</span>
      <span v-if="syncing" class="alerts__sync">对账中…</span>
      <span v-else-if="syncError" class="alerts__sync alerts__sync--bad">{{ syncError }}</span>
      <button class="alerts__refresh" type="button" data-testid="alert-refresh" @click="refresh()">对账</button>
      <button v-if="pendingOnly" class="alerts__refresh" type="button" @click="pendingOnly = false">全部在案</button>
    </header>

    <div class="alerts__seat" :aria-busy="checkingSeat">
      <form class="alerts__seat-form" @submit.prevent="checkSeat(seatDraft.trim(), true)">
        <label class="alerts__seat-label" :for="seatInputId">值班席位</label>
        <input
          :id="seatInputId"
          ref="seatInput"
          v-model="seatDraft"
          class="alerts__seat-input"
          data-testid="alert-seat"
          placeholder="输入已配置的席位编号"
          autocomplete="off"
          spellcheck="false"
          maxlength="32"
          :disabled="checkingSeat || busyId !== ''"
          :aria-describedby="`${seatInputId}-status`"
        />
        <button
          class="alert__btn"
          :class="{ 'alert__btn--primary': !confirmedSeat || seatDraft.trim() !== operatorSeat }"
          type="submit"
          data-testid="alert-seat-confirm"
          :disabled="checkingSeat || busyId !== ''"
        >
          {{ checkingSeat ? '验证中' : '确认席位' }}
        </button>
      </form>
      <div :id="`${seatInputId}-status`" class="alerts__seat-status" aria-live="polite">
        <p v-if="confirmedSeat" class="alerts__seat-current" data-testid="alert-seat-current">当前使用 {{ confirmedSeat }}</p>
        <p v-if="seatError" class="alerts__message" data-testid="alert-seat-error">{{ seatError }}</p>
        <p v-else-if="!confirmedSeat" class="alerts__seat-help">确认后可用于告警接手、任务认领和助手处置。</p>
        <button v-if="operatorSeat" class="alerts__seat-clear" type="button" :disabled="checkingSeat || busyId !== ''" @click="leaveSeat">
          退出席位
        </button>
      </div>
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
            <span v-if="card.drill" class="alert__flag">演练</span>
            <span v-if="card.status === 'escalated'" class="alert__flag">已升级</span>
            <span v-if="card.status === 'acked'" class="alert__flag alert__flag--muted">已接手</span>
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
    <p v-if="shown.length === 0" class="alerts__empty">
      {{ pendingOnly ? '当前没有待确认的红色或橙色告警。' : '当前没有在案告警。新预警发布后会自动出现在这里。' }}
    </p>
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
  flex-wrap: wrap;
  align-items: center;
  gap: var(--aeg-space-s2);
}
.alerts__title {
  font-size: var(--aeg-font-sm);
  font-weight: var(--aeg-weight-semibold);
  color: var(--aeg-color-text);
}
.alerts__count {
  padding: 0 var(--aeg-space-s2);
  border-radius: var(--aeg-radius-pill);
  background: var(--aeg-tag-red-bg);
  color: var(--aeg-tag-red-text);
  font-size: var(--aeg-font-sm);
  font-variant-numeric: tabular-nums;
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
  font-size: var(--aeg-font-sm);
  min-height: 28px;
  padding: 2px 8px;
  cursor: pointer;
}
.alerts__refresh + .alerts__refresh { margin-left: 0; }

.alerts__seat {
  display: grid;
  gap: var(--aeg-space-s1);
}
.alerts__seat-form,
.alerts__seat-status {
  display: flex;
  align-items: center;
  gap: var(--aeg-space-s2);
}
.alerts__seat-status {
  flex-wrap: wrap;
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-text-secondary);
}
.alerts__seat-current,
.alerts__seat-help {
  margin: 0;
}
.alerts__seat-current {
  color: var(--aeg-color-text);
  font-weight: var(--aeg-weight-semibold);
}
.alerts__seat-clear {
  margin-left: auto;
  padding: 2px 0;
  border: 0;
  color: inherit;
  background: transparent;
  text-decoration: underline;
  cursor: pointer;
}
.alerts__seat-label {
  font-size: var(--aeg-font-sm);
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
  min-height: 28px;
  padding: var(--aeg-space-s1) var(--aeg-space-s2);
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
  border-radius: var(--aeg-radius-md);
  background: var(--aeg-color-sunken);
}
.alert--drill {
  border-left-color: var(--aeg-tag-orange-line);
}
.alert__level {
  flex: none;
  font-size: var(--aeg-font-xs);
  font-weight: var(--aeg-weight-semibold);
  white-space: nowrap;
  align-self: flex-start;
  padding: 2px var(--aeg-space-s1);
  border-radius: var(--aeg-radius-sm);
  background: var(--aeg-color-overview-panel);
}
.alert__body {
  flex: 1;
  min-width: 0;
}
.alert__headline {
  margin: 0;
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: var(--aeg-space-s2);
  font-size: var(--aeg-font-sm);
  color: var(--aeg-color-text);
  overflow-wrap: anywhere;
}
.alert__flag {
  flex: none;
  font-size: var(--aeg-font-xs);
  padding-inline: var(--aeg-space-s1);
  border-radius: var(--aeg-radius-pill);
  background: var(--aeg-tag-orange-bg);
  color: var(--aeg-tag-orange-text);
}
.alert__flag--muted {
  background: var(--aeg-color-overview-panel);
  color: var(--aeg-color-text-secondary);
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
  flex-wrap: wrap;
  gap: var(--aeg-space-s2);
  margin-top: var(--aeg-space-s2);
}
.alert__btn {
  min-height: 26px;
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
  font-size: var(--aeg-font-sm);
  color: var(--aeg-color-text-muted);
}
</style>
