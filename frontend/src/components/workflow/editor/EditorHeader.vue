<script setup lang="ts">
/**
 * 沉浸式编辑器头部（56px，对齐 Dify 工作流 `top-14` 头部）：返回、可改名标题、保存态、版本与操作。
 *
 * 页面级错误/冲突不塞进这条 56px：它们各带按钮，放画布浮层（见 WorkflowView）；
 * 这里只回答“这版画布存没存上、是第几版、有几张等签”。
 */
import { ref } from 'vue'

const props = defineProps<{
  name: string
  saveTone: 'ok' | 'warn' | 'idle' | 'bad'
  saveText: string
  saveTitle: string
  version: number | null
  waiting: number
  polling: boolean
  canPublish: boolean
  busy: boolean
}>()

const emit = defineEmits<{
  rename: [name: string]
  save: []
  publish: []
  refresh: []
  newDraft: []
  layout: []
  fit: []
  openPanel: [panel: 'run' | 'def']
}>()

const moreRef = ref<HTMLDetailsElement | null>(null)

function rename(event: Event): void {
  const input = event.target as HTMLInputElement
  const next = input.value.trim()
  if (next.length < 2) {
    /* 名称下限与后端一致（2 字）；不合法就退回旧值，别让输入框停在一个必保存失败的名字上。 */
    input.value = props.name
    return
  }
  emit('rename', next)
}

function run(action: () => void): void {
  moreRef.value?.removeAttribute('open')
  action()
}
</script>

<template>
  <header class="ed-head" data-testid="editor-header">
    <div class="ed-head__left">
      <router-link class="ed-head__back" to="/dashboard" aria-label="返回态势总览" title="返回态势总览">←</router-link>
      <input class="ed-head__name" :value="name" maxlength="64" aria-label="流程名称" @change="rename" />
      <span class="ed-head__version">{{ version === null ? '未发布' : `v${version}` }}</span>
      <span class="ed-head__save" :class="`is-${saveTone}`" :title="saveTitle">
        <span class="ed-head__save-dot" aria-hidden="true"></span>{{ saveText }}
      </span>
    </div>

    <div class="ed-head__right">
      <button type="button" class="ed-head__btn" data-testid="header-run" @click="emit('openPanel', 'run')">
        运行态
        <span v-if="waiting > 0" class="ed-head__count" data-testid="waiting-count">{{ waiting }}</span>
      </button>
      <button type="button" class="ed-head__btn" data-testid="header-def" @click="emit('openPanel', 'def')">
        定义
      </button>
      <span v-if="polling" class="ed-head__polling" title="实例状态轮询中">轮询中</span>
      <button type="button" class="ed-head__btn" @click="emit('refresh')">刷新实例</button>
      <button type="button" class="ed-head__btn" :disabled="busy" @click="emit('save')">保存</button>
      <button
        type="button"
        class="ed-head__btn is-primary"
        :disabled="!canPublish || busy"
        :title="canPublish ? '发布为不可变版本' : '画布还有待修正项或未保存内容'"
        @click="emit('publish')"
      >
        发布
      </button>
      <details ref="moreRef" class="ed-head__more">
        <summary class="ed-head__btn">更多</summary>
        <div class="ed-head__menu">
          <button type="button" @click="run(() => emit('newDraft'))">新建画布</button>
          <button type="button" @click="run(() => emit('layout'))">自动布局</button>
          <button type="button" @click="run(() => emit('fit'))">适配画布</button>
        </div>
      </details>
    </div>
  </header>
</template>

<style scoped>
.ed-head {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  height: var(--aeg-editor-header-height, 56px);
  padding: 0 12px;
  border-bottom: 1px solid var(--aeg-color-border);
  background: var(--aeg-color-editor-header);
  color: var(--aeg-color-text);
}
.ed-head__left,
.ed-head__right {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.ed-head__left {
  flex: 1 1 auto;
}
.ed-head__right {
  flex: 0 0 auto;
}
.ed-head__back {
  display: grid;
  flex: 0 0 auto;
  place-items: center;
  width: 28px;
  height: 28px;
  border-radius: 7px;
  color: var(--aeg-color-text-secondary);
  font-size: 16px;
  text-decoration: none;
}
.ed-head__back:hover {
  background: var(--aeg-color-sunken);
  color: var(--aeg-color-text);
}
.ed-head__name {
  width: 220px;
  min-width: 80px;
  height: 30px;
  padding: 0 8px;
  border: 1px solid transparent;
  border-radius: 7px;
  background: transparent;
  color: var(--aeg-color-text);
  font: inherit;
  font-size: 14px;
  font-weight: 600;
  text-overflow: ellipsis;
}
.ed-head__name:hover {
  border-color: var(--aeg-color-border);
}
.ed-head__name:focus {
  border-color: var(--aeg-color-brand);
  background: var(--aeg-color-editor-panel);
  outline: none;
  box-shadow: 0 0 0 2px var(--aeg-color-brand-line);
}
.ed-head__version {
  flex: 0 0 auto;
  padding: 1px 7px;
  border: 1px solid var(--aeg-color-border);
  border-radius: 999px;
  color: var(--aeg-color-text-muted);
  font-family: var(--aeg-font-mono);
  font-size: 11px;
}
.ed-head__save {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 5px;
  margin-left: 4px;
  color: var(--aeg-color-text-muted);
  font-size: 11px;
  white-space: nowrap;
}
.ed-head__save-dot {
  width: 6px;
  height: 6px;
  border-radius: 999px;
  background: var(--aeg-color-text-muted);
}
.ed-head__save.is-ok .ed-head__save-dot {
  background: var(--aeg-color-status-success);
}
.ed-head__save.is-warn .ed-head__save-dot {
  background: var(--aeg-color-status-warning);
}
.ed-head__save.is-bad .ed-head__save-dot {
  background: var(--aeg-color-status-danger);
}
.ed-head__save.is-bad {
  color: var(--aeg-color-status-danger);
}
.ed-head__btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  /* 34px：这条 56px 头部就是这一页的主入口区，全站"页头动作区按钮 ≥34px"的地板在这儿同样成立
     ——antd small 是 24px，值班台上点起来是文字链不是按钮。 */
  height: 34px;
  padding: 0 10px;
  border: 1px solid var(--aeg-color-border);
  border-radius: 8px;
  background: var(--aeg-color-editor-panel);
  color: var(--aeg-color-text-secondary);
  font: inherit;
  font-size: 12px;
  white-space: nowrap;
  cursor: pointer;
}
.ed-head__btn:hover:not(:disabled) {
  border-color: var(--aeg-color-brand-line);
  color: var(--aeg-color-text);
}
.ed-head__btn:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}
.ed-head__btn.is-primary {
  border-color: transparent;
  background: var(--aeg-color-brand);
  color: var(--aeg-color-text-inverse);
  font-weight: 600;
}
.ed-head__btn.is-primary:hover:not(:disabled) {
  background: var(--aeg-color-brand-hover);
  color: var(--aeg-color-text-inverse);
}
.ed-head__count {
  padding: 0 5px;
  border-radius: 999px;
  background: var(--aeg-color-brand-soft);
  color: var(--aeg-color-brand-hover);
  font-family: var(--aeg-font-mono);
  font-size: 10px;
  font-weight: 700;
}
.ed-head__polling {
  color: var(--aeg-color-text-muted);
  font-size: 11px;
  white-space: nowrap;
}
.ed-head__more {
  position: relative;
}
.ed-head__more > summary {
  list-style: none;
}
.ed-head__more > summary::-webkit-details-marker {
  display: none;
}
.ed-head__menu {
  position: absolute;
  top: 34px;
  right: 0;
  z-index: 30;
  display: flex;
  flex-direction: column;
  min-width: 128px;
  padding: 4px;
  border: 1px solid var(--aeg-color-border);
  border-radius: 10px;
  background: var(--aeg-color-editor-panel);
  box-shadow: var(--aeg-shadow-lg);
}
.ed-head__menu button {
  height: 34px;
  padding: 0 10px;
  border: none;
  border-radius: 7px;
  background: transparent;
  color: var(--aeg-color-text-secondary);
  font: inherit;
  font-size: 12px;
  text-align: left;
  cursor: pointer;
}
.ed-head__menu button:hover {
  background: var(--aeg-color-sunken);
  color: var(--aeg-color-text);
}
@media (max-width: 900px) {
  .ed-head__name {
    width: 140px;
  }
  .ed-head__save,
  .ed-head__polling {
    display: none;
  }
}
</style>
