/**
 * 键盘焦点围栏：焦点在容器里循环，Esc 交给调用方去关。
 *
 * 为什么要有它：真机量过——预警详情抽屉打开后按 Tab，两次之内焦点就溜到 `body`，
 * 接着开始走顶栏的链接（`a.brand-link`、七个 `a.flow-step`），也就是**人在抽屉里、
 * 键盘在页面外**；而 Esc 又关不掉（按键监听挂在抽屉上，焦点不在里面就收不到）。
 * antd-vue 的 Modal 自带焦点锁，Drawer 没有，所以这一层自己补。
 */
const FOCUSABLE = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

export interface FocusTrapHandle {
  detach: () => void
}

export function attachFocusTrap(root: HTMLElement | null, onEscape?: () => void): FocusTrapHandle {
  if (root === null) return { detach: () => {} }
  const box = root

  function onKeyDown(event: KeyboardEvent): void {
    if (event.key === 'Escape') {
      onEscape?.()
      return
    }
    if (event.key !== 'Tab') return
    /* 可见性过滤用 `hidden`/`aria-hidden` 而不是 `offsetParent`：jsdom 里 offsetParent 永远是 null，
       单测会退化成"一个可聚焦元素都没找到"（真机与单测行为不一致的那种坑）。 */
    const items = [...box.querySelectorAll<HTMLElement>(FOCUSABLE)].filter(
      (el) => !el.hasAttribute('hidden') && el.getAttribute('aria-hidden') !== 'true',
    )
    if (items.length === 0) return
    const first = items[0] as HTMLElement
    const last = items[items.length - 1] as HTMLElement
    const active = document.activeElement as HTMLElement | null
    if (event.shiftKey && (active === first || !box.contains(active))) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && active === last) {
      event.preventDefault()
      first.focus()
    } else if (!box.contains(active)) {
      // 焦点已经漏出去了：立刻抓回来，否则键盘用户再也回不到这个弹层
      event.preventDefault()
      first.focus()
    }
  }

  document.addEventListener('keydown', onKeyDown, true)
  // 挂上的那一刻就把焦点拉进容器（抽屉本身没有可聚焦元素时，浏览器会停在 body）
  const firstItem = root.querySelector<HTMLElement>(FOCUSABLE)
  if (firstItem) firstItem.focus()
  else root.setAttribute('tabindex', '-1')

  return {
    detach: () => {
      document.removeEventListener('keydown', onKeyDown, true)
    },
  }
}
