<script setup lang="ts">
/**
 * 画布备注（对应 Dify `note-node`，220×140）：只存在于 layout.notes，不参与执行。
 *
 * 它是真正的 Vue Flow 节点：视口平移/缩放、叠放次序与拖动都由画布统一处理，
 * 文本与颜色直接写回 document store，随草稿自动保存。
 */
import type { NodeProps } from '@vue-flow/core'
import { computed } from 'vue'

import type { StickyNote, StickyNoteColor } from '@/utils/graph/document'

import { useEditorContext } from './context'

const props = defineProps<NodeProps<{ note: StickyNote }>>()

const { document: documentStore, editor } = useEditorContext()

const TONES: Record<StickyNoteColor, { bg: string; line: string; text: string }> = {
  amber: { bg: 'var(--aeg-tag-orange-bg)', line: 'var(--aeg-tag-orange-line)', text: 'var(--aeg-tag-orange-text)' },
  blue: { bg: 'var(--aeg-tag-blue-bg)', line: 'var(--aeg-tag-blue-line)', text: 'var(--aeg-tag-blue-text)' },
  green: { bg: 'var(--aeg-tag-green-bg)', line: 'var(--aeg-tag-green-line)', text: 'var(--aeg-tag-green-text)' },
  violet: {
    bg: 'color-mix(in srgb, var(--aeg-color-editor-state-awaiting) 14%, var(--aeg-color-editor-panel))',
    line: 'color-mix(in srgb, var(--aeg-color-editor-state-awaiting) 45%, transparent)',
    text: 'var(--aeg-color-editor-state-awaiting)',
  },
}

const COLORS: StickyNoteColor[] = ['amber', 'blue', 'green', 'violet']

const note = computed(() => props.data.note)
const tone = computed(() => TONES[note.value.color])

const style = computed(() => ({
  width: `${note.value.width}px`,
  height: `${note.value.height}px`,
  '--note-bg': tone.value.bg,
  '--note-line': tone.value.line,
  '--note-text': tone.value.text,
}))

function onText(event: Event): void {
  documentStore.updateNote(note.value.note_id, { text: (event.target as HTMLTextAreaElement).value })
}

function setColor(color: StickyNoteColor): void {
  documentStore.updateNote(note.value.note_id, { color })
}

function remove(): void {
  documentStore.removeNote(note.value.note_id)
  if (editor.selectedNoteId === note.value.note_id) editor.selectNote(null)
}
</script>

<template>
  <div
    class="ed-note"
    :class="{ 'is-selected': props.selected }"
    :style="style"
    data-testid="canvas-note"
    :data-note-id="note.note_id"
  >
    <header class="ed-note__head" title="拖动备注">
      <span class="ed-note__grip" aria-hidden="true">⋮⋮</span>
      <span class="ed-note__id">{{ note.note_id }}</span>
      <button type="button" class="ed-note__delete nodrag nopan" aria-label="删除备注" @click.stop="remove">×</button>
    </header>
    <textarea
      class="ed-note__text nodrag nopan nowheel"
      :value="note.text"
      placeholder="输入备注…"
      aria-label="备注内容"
      maxlength="2000"
      @input="onText"
      @pointerdown.stop
      @keydown.stop
    />
    <footer class="ed-note__foot">
      <button
        v-for="color in COLORS"
        :key="color"
        type="button"
        class="ed-note__swatch nodrag nopan"
        :class="[`is-${color}`, { 'is-active': note.color === color }]"
        :aria-label="`备注颜色 ${color}`"
        :aria-pressed="note.color === color"
        @click.stop="setColor(color)"
      />
    </footer>
  </div>
</template>

<style scoped>
.ed-note {
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid var(--note-line);
  border-radius: 12px;
  background: var(--note-bg);
  box-shadow: var(--aeg-shadow-sm);
  color: var(--note-text);
}
.ed-note.is-selected {
  border-color: var(--aeg-color-brand);
  box-shadow: 0 0 0 2px var(--aeg-color-brand-line);
}
.ed-note__head {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 6px;
  height: 24px;
  padding: 0 4px 0 8px;
  border-bottom: 1px solid color-mix(in srgb, var(--note-line) 65%, transparent);
  cursor: grab;
}
.ed-note__head:active {
  cursor: grabbing;
}
.ed-note__grip {
  color: color-mix(in srgb, var(--note-text) 55%, transparent);
  font-size: 10px;
  letter-spacing: -2px;
}
.ed-note__id {
  flex: 1;
  overflow: hidden;
  font-family: var(--aeg-font-mono);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ed-note__delete {
  display: grid;
  place-items: center;
  width: 20px;
  height: 20px;
  padding: 0;
  border: none;
  border-radius: 5px;
  background: transparent;
  color: var(--note-text);
  font-size: 14px;
  line-height: 1;
  cursor: pointer;
}
.ed-note__delete:hover {
  background: color-mix(in srgb, var(--note-line) 35%, transparent);
}
.ed-note__text {
  flex: 1;
  min-height: 0;
  margin: 0;
  padding: 6px 8px;
  border: none;
  background: transparent;
  color: var(--note-text);
  font: inherit;
  font-size: 12px;
  line-height: 1.5;
  resize: none;
  outline: none;
}
.ed-note__text::placeholder {
  color: color-mix(in srgb, var(--note-text) 55%, transparent);
}
.ed-note__foot {
  display: flex;
  flex: 0 0 auto;
  gap: 5px;
  padding: 4px 8px 6px;
}
.ed-note__swatch {
  width: 12px;
  height: 12px;
  padding: 0;
  border: 1px solid color-mix(in srgb, var(--note-text) 35%, transparent);
  border-radius: 999px;
  cursor: pointer;
}
.ed-note__swatch.is-amber {
  background: var(--aeg-tag-orange-bg);
}
.ed-note__swatch.is-blue {
  background: var(--aeg-tag-blue-bg);
}
.ed-note__swatch.is-green {
  background: var(--aeg-tag-green-bg);
}
.ed-note__swatch.is-violet {
  background: color-mix(in srgb, var(--aeg-color-editor-state-awaiting) 35%, var(--aeg-color-editor-panel));
}
.ed-note__swatch.is-active {
  outline: 2px solid var(--aeg-color-brand);
  outline-offset: 1px;
}
</style>
