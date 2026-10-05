<script setup lang="ts">
defineProps<{
  title: string
  badge?: string
  caption?: string
  /** 单字形图标（NexusMind 用 emoji 占这个位置，这里用汉字：离线、无版权、且中文台不违和） */
  icon?: string
}>()
</script>

<template>
  <section class="page-hero" :class="{ 'page-hero--icon': !!icon }">
    <div class="page-hero__header">
      <span v-if="icon" class="page-hero__icon" aria-hidden="true">{{ icon }}</span>
      <span v-else class="page-hero__deco" aria-hidden="true">◆</span>
      <h2 class="page-hero__title">{{ title }}</h2>
      <span v-if="badge" class="page-hero__badge">{{ badge }}</span>
      <div class="page-hero__actions">
        <slot name="actions" />
      </div>
    </div>
    <p v-if="caption" class="page-hero__caption">{{ caption }}</p>
    <div v-if="$slots.stats" class="page-hero__stats">
      <slot name="stats" />
    </div>
  </section>
</template>

<!-- 深色横幅移植自 NexusMind：藏青渐变 + 两团辉光 + 等宽字徽标（.global-status-card），
     数字托盘是它的 .overview-stats（黑 15% 底板 + 白色等宽大数 + 小字大写标签）。 -->
<style scoped>
.page-hero {
  position: relative;
  overflow: hidden;
  background: linear-gradient(145deg, #0d2b3e 0%, #0f3d52 50%, #0d4a62 100%);
  border-radius: 16px;
  padding: 16px 22px;
  margin-bottom: 16px;
  box-shadow: 0 8px 30px rgba(15, 60, 82, 0.35), 0 0 0 1px rgba(37, 99, 235, 0.15);
}
.page-hero::before {
  content: '';
  position: absolute;
  top: -40px;
  right: -40px;
  width: 160px;
  height: 160px;
  background: radial-gradient(circle, rgba(37, 99, 235, 0.2) 0%, transparent 70%);
  pointer-events: none;
}
.page-hero::after {
  content: '';
  position: absolute;
  bottom: -20px;
  left: -20px;
  width: 100px;
  height: 100px;
  background: radial-gradient(circle, rgba(37, 99, 235, 0.12) 0%, transparent 70%);
  pointer-events: none;
}
.page-hero__header {
  position: relative;
  z-index: 1;
  display: flex;
  align-items: center;
  gap: 10px;
}
.page-hero__deco {
  font-size: 12px;
  color: #5aa2ff;
}
/* 图标芯片（对标 .tool-icon-wrapper：34px / 圆角 12 / 渐变底 + 同色雾投影） */
.page-hero__icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border-radius: 12px;
  background: linear-gradient(135deg, #2563eb, #1d4ed8);
  box-shadow: 0 8px 18px rgba(37, 99, 235, 0.32), inset 0 0 0 1px rgba(255, 255, 255, 0.22);
  color: #fff;
  font-size: 15px;
  font-weight: 700;
}
.page-hero--icon .page-hero__caption {
  /* 有芯片时标题从 44px 起，说明文字跟着对齐，不然左边缘两条线不齐 */
  margin-left: 44px;
}
.page-hero__title {
  margin: 0;
  font-size: 14px;
  font-weight: 700;
  color: #fff;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.page-hero__badge {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  font-weight: 700;
  color: #7cb3ff;
  background: rgba(37, 99, 235, 0.18);
  border: 1px solid rgba(37, 99, 235, 0.35);
  padding: 2px 8px;
  border-radius: 4px;
  letter-spacing: 0.1em;
  animation: page-hero-pulse 2s ease-in-out infinite;
}
@keyframes page-hero-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.6; }
}
.page-hero__actions {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 10px;
  /* 槽位内容由各页模板提供，颜色与字号靠继承落到横幅里。 */
  color: rgba(255, 255, 255, 0.78);
  font-size: 12px;
}
.page-hero__caption {
  position: relative;
  z-index: 1;
  margin: 8px 0 0 22px;
  font-size: 12px;
  color: rgba(255, 255, 255, 0.72);
}
.page-hero__stats {
  position: relative;
  z-index: 1;
  display: flex;
  align-items: center;
  justify-content: space-around;
  margin-top: 12px;
  padding: 10px 6px;
  border-radius: 10px;
  background: rgba(0, 0, 0, 0.15);
}
</style>
