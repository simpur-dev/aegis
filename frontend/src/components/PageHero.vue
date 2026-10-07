<script setup lang="ts">
/**
 * 页头（阶段 1 后半·旧视觉停用）。
 *
 * 这里原来是 NexusMind 那块藏青横幅：`linear-gradient(145deg,…)` + 两团 `radial-gradient` 辉光
 * + 标题前的 ◆ + 单字形渐变图标芯片 + 徽标无限脉冲 + 黑 15% 的数字托盘。
 * 判词是"糟糕、不专业"，2026-10-07 拍板**现在停用**，所以这些一律没有，只留三件有功能目的的东西：
 * 页名（这屏在讲什么）、状态语（现在怎么样了）、动作区（能干什么）。
 * 单字形图标退役：标题已经说了页名，那颗芯片只是装饰。
 *
 * 两条实测约束保留，只是换了宿主选择器：
 * ① 动作区里的按钮 `min-height: 36px`——窄屏上 antd small 的 24px 高在横幅里点不准（真机量的）；
 * ② 状态语**可缩不可撑**，挤不下省略号收尾、完整那句进 `title`，不许把动作钮推出行。
 */
defineProps<{
  title: string
  badge?: string
  caption?: string
  /** 一眼看得出的当下状态（只写"现在怎么样了"，不复述标题；tone 由数据决定，不许拿它当装饰）。 */
  status?: { tone: 'ok' | 'warn' | 'bad' | 'idle'; text: string }
}>()
</script>

<template>
  <header class="page-head">
    <div class="page-head__row">
      <h2 class="page-head__title">{{ title }}</h2>
      <span v-if="badge" class="page-head__badge">{{ badge }}</span>
      <span v-if="status" class="page-head__status" :class="`is-${status.tone}`" :title="status.text">
        <span class="page-head__status-dot" aria-hidden="true"></span>
        <span class="page-head__status-text">{{ status.text }}</span>
      </span>
      <div class="page-head__actions">
        <slot name="actions" />
      </div>
    </div>
    <p v-if="caption" class="page-head__caption">{{ caption }}</p>
    <div v-if="$slots.stats" class="page-head__stats">
      <slot name="stats" />
    </div>
  </header>
</template>

<style scoped>
.page-head {
  margin-bottom: var(--aeg-space-s4);
}
.page-head__row {
  display: flex;
  align-items: center;
  gap: var(--aeg-space-s3);
}
.page-head__title {
  margin: 0;
  font-size: var(--aeg-font-lg);
  font-weight: var(--aeg-weight-semibold);
  line-height: var(--aeg-lh-snug);
  color: var(--aeg-color-text);
}
/* 徽标是"这一页的数据从哪来"的口径标记（实时/实测/离线三维…），不是装饰：
   中性灰的等宽小牌，不脉冲、不发光——原来那支 infinite 脉冲让 10px 的字每两秒淡到 3.08:1。 */
.page-head__badge {
  flex: 0 0 auto;
  padding: 2px var(--aeg-space-s2);
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-sm);
  background: var(--aeg-color-sunken);
  color: var(--aeg-color-text-secondary);
  font-family: var(--aeg-font-mono);
  font-size: var(--aeg-font-xs);
  font-weight: var(--aeg-weight-bold);
  letter-spacing: 0.06em;
}
.page-head__status {
  display: inline-flex;
  align-items: center;
  gap: var(--aeg-space-s1);
  flex: 0 1 auto;
  min-width: 0;
  font-size: var(--aeg-font-sm);
  color: var(--aeg-color-text-secondary);
}
.page-head__status-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.page-head__status-dot {
  flex: 0 0 auto;
  width: 8px;
  height: 8px;
  border-radius: var(--aeg-radius-pill);
  background: var(--aeg-color-border-strong);
}
.page-head__status.is-ok .page-head__status-dot {
  background: var(--aeg-color-status-success);
}
.page-head__status.is-warn .page-head__status-dot {
  background: var(--aeg-color-status-warning);
}
.page-head__status.is-bad .page-head__status-dot {
  background: var(--aeg-color-status-danger);
}
.page-head__actions {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: var(--aeg-space-s2);
}
/* 动作区里是各页模板塞进来的 antd 按钮，scoped 样式够不到，必须 :deep。
   36px 这个下限是真机量的：small 档 24px 高在窄屏上点不准。 */
.page-head__actions :deep(.ant-btn) {
  min-height: 36px;
}
.page-head__caption {
  margin: var(--aeg-space-s2) 0 0;
  font-size: var(--aeg-font-sm);
  line-height: var(--aeg-lh-normal);
  color: var(--aeg-color-text-secondary);
  max-width: 72ch;
}
.page-head__stats {
  display: flex;
  flex-flow: row wrap;
  /* 顶端对齐：托盘里每格内容高度不等（带环形 KPI 的那格高出 ~50px），
     center 会把 24px 的大数字抬到比左右两格高 25px，整排读数就散了（真机 @1440 量过）。 */
  align-items: flex-start;
  gap: var(--aeg-space-s4);
  margin-top: var(--aeg-space-s3);
  padding: var(--aeg-space-s3);
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-md);
  background: var(--aeg-color-alter);
}
</style>
