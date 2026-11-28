<script setup lang="ts">
import PageHero from '@/components/PageHero.vue'

defineProps<{ title: string; badge?: string; caption: string }>()
</script>

<template>
  <PageHero class="data-workspace-heading data-workspace-panel" surface hide-ridge v-bind="$props">
    <template #heading-icon>
      <span class="data-workspace-heading__icon" aria-hidden="true"><slot name="icon" /></span>
    </template>
    <template #actions>
      <slot name="actions" />
    </template>
  </PageHero>
</template>

<style scoped>
.data-workspace-heading {
  display: grid;
  grid-template-columns: auto minmax(0, max-content) auto minmax(0, 1fr) auto;
  grid-template-areas: 'icon title badge . action' 'icon caption caption caption action';
  align-content: center;
  align-items: center;
  gap: 3px 15px;
  min-height: 96px;
  margin: 0;
  padding: 10px 22px 10px 28px;
}
.data-workspace-heading :deep(.page-head__row) { display: contents; }
.data-workspace-heading :deep(.page-head__title) {
  grid-area: title;
  font-size: clamp(25px, 2vw, 34px);
}
.data-workspace-heading :deep(.page-head__badge) { grid-area: badge; justify-self: start; }
.data-workspace-heading :deep(.page-head__caption) { grid-area: caption; margin: 0; font-size: 16px; }
.data-workspace-heading :deep(.page-head__actions) { grid-area: action; margin: 0; align-items: center; }
.data-workspace-heading :deep(.ant-btn) { min-height: 44px; min-width: 106px; }
.data-workspace-heading__icon {
  position: relative;
  display: grid;
  grid-area: icon;
  place-items: center;
  color: var(--aeg-color-brand);
  font-size: 36px;
}

html[data-theme='dark'] .data-workspace-heading { padding-inline-start: 58px; }
html[data-theme='dark'] .data-workspace-heading::after {
  position: absolute;
  z-index: -1;
  inset: 0 12% 0 28%;
  background: url('/backgrounds/monitor-heading-mesh.webp') center 42% / 100% auto no-repeat;
  mask-image: linear-gradient(90deg, transparent 2%, black 23%, black 85%, transparent);
  opacity: .7;
  pointer-events: none;
  content: '';
}
html[data-theme='dark'] .data-workspace-heading__icon {
  color: var(--aeg-color-text);
  filter: drop-shadow(0 2px 8px var(--aeg-color-command-glow));
}
html[data-theme='dark'] .data-workspace-heading__icon::before {
  position: absolute;
  inset: -4px auto -4px -28px;
  width: 5px;
  border-radius: 5px;
  background: linear-gradient(var(--aeg-color-terrain-accent), var(--data-ice) 50%, var(--aeg-color-brand-active));
  box-shadow: 0 0 13px var(--data-ice);
  content: '';
}
html[data-theme='dark'] .data-workspace-heading :deep(.ant-btn-default) {
  border-color: var(--data-edge);
  background: linear-gradient(180deg, var(--aeg-color-command-panel), var(--data-inset));
}
html[data-theme='dark'] .data-workspace-heading :deep(.ant-btn-default:hover) {
  border-color: var(--data-ice);
  background: var(--aeg-color-command-raised);
}

@container (max-width: 620px) {
  .data-workspace-heading,
  html[data-theme='dark'] .data-workspace-heading {
    grid-template-columns: auto minmax(0, 1fr) auto;
    grid-template-areas: 'icon title badge' 'caption caption caption' 'action action action';
    justify-items: start;
    min-height: 0;
    padding: 12px;
    gap: 6px 8px;
  }
  .data-workspace-heading__icon { font-size: 22px; }
  .data-workspace-heading :deep(.page-head__title) { font-size: 23px; }
  .data-workspace-heading :deep(.page-head__caption) {
    min-block-size: 2lh;
    font-size: var(--aeg-font-sm);
  }
  .data-workspace-heading :deep(.ant-btn) { min-height: 38px; }
  html[data-theme='dark'] .data-workspace-heading__icon::before { display: none; }
  html[data-theme='dark'] .data-workspace-heading::after { inset-inline: 24% 1%; opacity: .52; }
}
</style>
