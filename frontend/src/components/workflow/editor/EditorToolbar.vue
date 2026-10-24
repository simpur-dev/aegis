<script setup lang="ts">
/** 画布左上角的浮动工具条：添加节点 / 备注 / 自动布局 / 适配画布（对齐 Dify 画布浮层控件的位置与尺寸）。 */
defineProps<{ disabled?: boolean }>()
const emit = defineEmits<{ add: [event: MouseEvent]; note: []; layout: []; fit: [] }>()
</script>

<template>
  <div class="ed-toolbar" role="toolbar" aria-label="画布工具">
    <button
      type="button"
      class="ed-toolbar__btn is-primary"
      :disabled="disabled"
      data-testid="toolbar-add"
      @click="emit('add', $event)"
    >
      <span class="ed-toolbar__plus" aria-hidden="true">+</span>添加节点
    </button>
    <button type="button" class="ed-toolbar__btn" :disabled="disabled" data-testid="toolbar-note" @click="emit('note')">
      备注
    </button>
    <button
      type="button"
      class="ed-toolbar__btn"
      :disabled="disabled"
      data-testid="toolbar-layout"
      @click="emit('layout')"
    >
      自动布局
    </button>
    <button type="button" class="ed-toolbar__btn" @click="emit('fit')">适配画布</button>
  </div>
</template>

<style scoped>
.ed-toolbar {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 4px;
  border: 1px solid var(--aeg-color-border);
  border-radius: 10px;
  background: var(--aeg-color-editor-panel);
  box-shadow: var(--aeg-shadow-md);
}
.ed-toolbar__btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 28px;
  padding: 0 10px;
  border: 1px solid transparent;
  border-radius: 7px;
  background: transparent;
  color: var(--aeg-color-text-secondary);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}
.ed-toolbar__btn:hover:not(:disabled) {
  background: var(--aeg-color-sunken);
  color: var(--aeg-color-text);
}
.ed-toolbar__btn:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}
.ed-toolbar__btn.is-primary {
  background: var(--aeg-color-brand);
  color: var(--aeg-color-text-inverse);
}
.ed-toolbar__btn.is-primary:hover:not(:disabled) {
  background: var(--aeg-color-brand-hover);
  color: var(--aeg-color-text-inverse);
}
.ed-toolbar__plus {
  font-size: 15px;
  line-height: 1;
}
</style>
