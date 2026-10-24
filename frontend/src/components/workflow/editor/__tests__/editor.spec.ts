import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createNodeDef } from '@/components/workflow/registry'
import type { NodeType } from '@/utils/graph'

import BlockSelector from '../BlockSelector.vue'
import EditorHeader from '../EditorHeader.vue'
import EditorToolbar from '../EditorToolbar.vue'
import NodePanel from '../NodePanel.vue'
import { createEditorContext, EDITOR_CONTEXT_KEY, type EditorContext } from '../context'

function context(): EditorContext {
  const created = createEditorContext()
  created.document.autoSaveEnabled = false
  return created
}

function mountedContext(ctx: EditorContext): { provide: Record<symbol, unknown> } {
  return { provide: { [EDITOR_CONTEXT_KEY]: ctx } }
}

function openerButton(): HTMLButtonElement {
  const button = document.createElement('button')
  document.body.append(button)
  button.focus()
  return button
}

beforeEach(() => {
  setActivePinia(createPinia())
  document.body.innerHTML = ''
  /* jsdom 没有实现这几支滚动 API；面板切页签与方向键高亮会调用它们。 */
  Element.prototype.scrollTo = vi.fn()
  Element.prototype.scrollIntoView = vi.fn()
})

describe('块选择器', () => {
  it('搜索按名称/类型/描述过滤；分类页签只留本类', async () => {
    const ctx = context()
    const wrapper = mount(BlockSelector, {
      props: { request: { x: 400, y: 300 } },
      global: mountedContext(ctx),
      attachTo: document.body,
    })
    const rows = (): string[] =>
      wrapper.findAll('.block-selector__row').map((row) => row.find('.block-selector__label').text())
    expect(rows().length, '块选择器一个块都没渲染——判据在空跑').toBeGreaterThan(10)

    await wrapper.find('.block-selector__input').setValue('事件通报')
    expect(rows()).toEqual(['事件通报'])

    await wrapper.find('.block-selector__input').setValue('')
    const humanTab = wrapper.findAll('.block-selector__tab').find((tab) => tab.text() === '人工介入')
    expect(humanTab, '分类页签里没有「人工介入」').toBeDefined()
    await humanTab?.trigger('click')
    const humanRows = wrapper.findAll('.block-selector__row')
    expect(humanRows.length, '人工介入分类一个块都没有').toBeGreaterThan(0)
    expect(wrapper.findAll('.block-selector__icon.is-human')).toHaveLength(humanRows.length)
    wrapper.unmount()
  })

  it('方向键移动高亮、Enter 选中高亮项、Escape 关闭；打开时搜索框拿到焦点', async () => {
    const ctx = context()
    const wrapper = mount(BlockSelector, {
      props: { request: { x: 400, y: 300 } },
      global: mountedContext(ctx),
      attachTo: document.body,
    })
    await nextTick()
    await nextTick()
    const input = wrapper.find('.block-selector__input')
    expect(document.activeElement, '打开块选择器时搜索框没拿到焦点').toBe(input.element)

    await input.trigger('keydown', { key: 'ArrowDown' })
    const active = wrapper.find('.block-selector__row.is-active')
    expect(active.exists(), '方向键之后没有任何一行是高亮态').toBe(true)
    const activeType = active.find('.block-selector__type').text()
    await input.trigger('keydown', { key: 'Enter' })
    expect(wrapper.emitted('select')?.[0]).toEqual([activeType])

    await input.trigger('keydown', { key: 'Escape' })
    expect(wrapper.emitted('close')).toHaveLength(1)
    wrapper.unmount()
  })

  it('已有开始节点 / 从边中点插入时禁用 Start，并把原因写在行尾', async () => {
    const ctx = context()
    ctx.document.addNode(createNodeDef('start', 'start_1'))
    const wrapper = mount(BlockSelector, {
      props: { request: { x: 400, y: 300 } },
      global: mountedContext(ctx),
      attachTo: document.body,
    })
    const startRow = wrapper
      .findAll('.block-selector__row')
      .find((row) => row.find('.block-selector__label').text() === '开始')
    expect(startRow, '块选择器里没有开始节点').toBeDefined()
    expect(startRow?.classes(), '已有开始节点时 Start 还可点').toContain('is-disabled')
    expect(startRow?.attributes('aria-disabled')).toBe('true')
    expect(startRow?.find('.block-selector__hint').text()).toBe('已有开始节点')
    await startRow?.trigger('click')
    expect(wrapper.emitted('select'), '点了禁用的 Start 仍然发出了选择事件').toBeUndefined()
    wrapper.unmount()

    const onEdge = mount(BlockSelector, {
      props: { request: { x: 400, y: 300, edge: { id: 'e1', source: 'a', target: 'b' } } },
      global: mountedContext(ctx),
      attachTo: document.body,
    })
    const edgeStart = onEdge
      .findAll('.block-selector__row')
      .find((row) => row.find('.block-selector__label').text() === '开始')
    expect(edgeStart?.find('.block-selector__hint').text()).toBe('不能插在链路中间')
    onEdge.unmount()
  })

  it('400px 浮层收在视口内；关闭后焦点还给打开它的按钮', async () => {
    const ctx = context()
    const opener = openerButton()
    const wrapper = mount(BlockSelector, {
      props: { request: { x: 900, y: 300 } },
      global: mountedContext(ctx),
      attachTo: document.body,
    })
    const style = wrapper.find('.block-selector').attributes('style') ?? ''
    expect(style, '块选择器不是 400px 宽').toContain('width: 400px')
    expect(style, '块选择器没有按锚点定位').toMatch(/left: \d+px/)
    expect(style, '块选择器没有按锚点定位').toMatch(/top: \d+px/)
    wrapper.unmount()
    expect(document.activeElement, '关闭块选择器后焦点没还给打开它的按钮').toBe(opener)
  })
})

describe('覆盖式编排面板', () => {
  it('三档页签可切换并 emit；无选中节点时给指路空态', async () => {
    const wrapper = mount(NodePanel, {
      props: { panel: 'node', waiting: 2, hasSelection: false },
      slots: { default: '<div data-testid="slot-body">节点参数</div>' },
    })
    expect(wrapper.find('.ed-panel__empty').text()).toContain('先在画布上点一个节点')
    expect(wrapper.find('[data-testid="slot-body"]').exists(), '无选中节点时仍渲染了节点内容').toBe(false)
    expect(wrapper.find('.ed-panel__count').text()).toBe('2')
    await wrapper.find('[data-testid="panel-def"]').trigger('click')
    await wrapper.find('[data-testid="panel-run"]').trigger('click')
    await wrapper.find('[data-testid="panel-close"]').trigger('click')
    expect(wrapper.emitted('select')).toEqual([['def'], ['run']])
    expect(wrapper.emitted('close')).toHaveLength(1)
  })

  it('有选中节点时渲染插槽；卸载时焦点还给打开按钮', async () => {
    const opener = openerButton()
    const wrapper = mount(NodePanel, {
      props: { panel: 'node', waiting: 0, hasSelection: true },
      slots: { default: '<div data-testid="slot-body">节点参数</div>' },
      attachTo: document.body,
    })
    expect(wrapper.find('[data-testid="slot-body"]').exists()).toBe(true)
    expect(wrapper.find('.ed-panel__empty').exists()).toBe(false)
    wrapper.unmount()
    expect(document.activeElement).toBe(opener)
  })
})

describe('编辑器头部与画布工具条', () => {
  it('改名校验与动作分发：短名回退、发布禁用时不 emit', async () => {
    const wrapper = mount(EditorHeader, {
      props: {
        name: '防控链路',
        saveTone: 'ok',
        saveText: '已保存',
        saveTitle: '刚刚保存',
        version: 3,
        waiting: 2,
        polling: true,
        canPublish: false,
        busy: false,
      },
      global: { stubs: { RouterLink: true } },
    })
    const name = wrapper.find('.ed-head__name')
    await name.setValue('短')
    expect(wrapper.emitted('rename'), '一个字的名字不该被提交').toBeUndefined()
    await name.setValue('新的链路名')
    expect(wrapper.emitted('rename')?.[0]).toEqual(['新的链路名'])

    expect(wrapper.find('.ed-head__version').text()).toBe('v3')
    expect(wrapper.find('[data-testid="waiting-count"]').text()).toBe('2')
    expect(wrapper.find('.ed-head__polling').exists()).toBe(true)

    const publish = wrapper.findAll('.ed-head__btn').find((button) => button.text() === '发布')
    expect(publish?.attributes('disabled'), '画布不可发布时发布按钮仍可点').toBeDefined()
    await publish?.trigger('click')
    expect(wrapper.emitted('publish'), '禁用的发布按钮仍然 emit').toBeUndefined()

    await wrapper.find('[data-testid="header-run"]').trigger('click')
    await wrapper.find('[data-testid="header-def"]').trigger('click')
    expect(wrapper.emitted('openPanel')).toEqual([['run'], ['def']])
    const save = wrapper.findAll('.ed-head__btn').find((button) => button.text() === '保存')
    await save?.trigger('click')
    expect(wrapper.emitted('save')).toHaveLength(1)
    wrapper.unmount()
  })

  it('工具条五个按钮各发各的事件，禁用态真的挂上 disabled', async () => {
    const disabled = mount(EditorToolbar, { props: { disabled: true } })
    const buttons = disabled.findAll('.ed-toolbar__btn')
    expect(buttons).toHaveLength(5)
    for (const button of buttons.slice(0, 3)) {
      expect(button.attributes('disabled'), `禁用时 ${button.text()} 仍可点`).toBeDefined()
    }
    expect(buttons[3]?.attributes('disabled'), '「适配画布」不需要定义，不该跟着禁用').toBeUndefined()
    expect(buttons[4]?.attributes('disabled'), '模式切换不该跟着禁用').toBeUndefined()
    disabled.unmount()

    const wrapper = mount(EditorToolbar)
    const live = wrapper.findAll('.ed-toolbar__btn')
    await live[0]?.trigger('click')
    await live[1]?.trigger('click')
    await live[2]?.trigger('click')
    await live[3]?.trigger('click')
    await live[4]?.trigger('click')
    expect(wrapper.emitted('add')).toHaveLength(1)
    expect(wrapper.emitted('note')).toHaveLength(1)
    expect(wrapper.emitted('layout')).toHaveLength(1)
    expect(wrapper.emitted('fit')).toHaveLength(1)
    expect(wrapper.emitted('mode')).toEqual([['hand']])

    await wrapper.setProps({ mode: 'hand' })
    await wrapper.findAll('.ed-toolbar__btn')[4]?.trigger('click')
    expect(wrapper.emitted('mode')).toEqual([['hand'], ['pointer']])
  })
})

/** 块选择器只列可编排类型：内部原语不该出现在搜索里（后端拒收，前端先别给）。 */
describe('块选择器与节点目录一致', () => {
  it('列出的每一个块都能在注册表里建出节点', () => {
    const ctx = context()
    const wrapper = mount(BlockSelector, {
      props: { request: { x: 400, y: 300 } },
      global: mountedContext(ctx),
      attachTo: document.body,
    })
    const types = wrapper.findAll('.block-selector__type').map((row) => row.text())
    expect(types.length).toBeGreaterThan(10)
    for (const type of types) {
      expect(() => createNodeDef(type as NodeType, `${type}_1`), `${type} 在块选择器里却建不出节点`).not.toThrow()
    }
    wrapper.unmount()
  })
})
