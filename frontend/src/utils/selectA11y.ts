/**
 * rc-select 无障碍同步层（antd-vue 4.2.6 上游缺陷的应用侧补丁）。
 *
 * 真机 axe 量到三处 critical，都落在 combobox input 上：
 * 1. `aria-label` 被 antd 落在外层 `.ant-select` div，input 本身无名（label）；
 * 2. input 缺 `aria-expanded`（aria-required-attr）；
 * 3. 关闭态 `aria-owns` / `aria-controls` / `aria-activedescendant` 指向尚未渲染的列表
 *    （aria-valid-attr-value）——下拉是点开才挂载的。
 *
 * 这里不改库、不降判据：把外层 label 抄给 input、expanded 跟开合走、关闭态摘掉悬空引用
 * （原值暂存 dataset，开回来再装回，键盘导航的 activedescendant 仍由 rc-select 自己更新）。
 * 一个 MutationObserver 盯 class/aria-label 变化与新增节点，抽屉里后挂的下拉也覆盖得到。
 */

const REFERENTIAL = ['aria-owns', 'aria-controls', 'aria-activedescendant'] as const

function syncOne(sel: Element): void {
  const input = sel.querySelector('input[role="combobox"]')
  if (!(input instanceof HTMLInputElement)) return
  const label = sel.getAttribute('aria-label')
  if (label && !input.hasAttribute('aria-label')) input.setAttribute('aria-label', label)
  const open = sel.classList.contains('ant-select-open')
  input.setAttribute('aria-expanded', String(open))
  for (const attr of REFERENTIAL) {
    const stashKey = `aeg${attr.replace(/-/g, '')}`
    if (open) {
      const saved = input.dataset[stashKey]
      if (saved !== undefined && !input.hasAttribute(attr)) input.setAttribute(attr, saved)
    } else {
      const value = input.getAttribute(attr)
      if (value !== null) {
        input.dataset[stashKey] = value
        input.removeAttribute(attr)
      }
    }
  }
}

export function syncSelectA11y(root: ParentNode = document): void {
  for (const sel of root.querySelectorAll('.ant-select')) syncOne(sel)
}

/** 装一次全站同步；返回卸载函数。App 外壳挂载时调用。 */
export function installSelectA11y(root: HTMLElement = document.body): () => void {
  syncSelectA11y(root)
  const observer = new MutationObserver((records) => {
    for (const record of records) {
      if (record.type === 'attributes') {
        const target = record.target
        if (target instanceof Element && target.classList.contains('ant-select')) syncOne(target)
        continue
      }
      for (const node of record.addedNodes) {
        if (!(node instanceof Element)) continue
        if (node.classList.contains('ant-select')) syncOne(node)
        else syncSelectA11y(node)
      }
    }
  })
  observer.observe(root, { subtree: true, childList: true, attributes: true, attributeFilter: ['class', 'aria-label'] })
  return () => observer.disconnect()
}