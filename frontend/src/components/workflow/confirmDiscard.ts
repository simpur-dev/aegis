import { Modal } from 'ant-design-vue'

/**
 * 覆盖画布之前先问一句。
 *
 * 真机上"新建画布"是一点就走：摆了五个节点还没保存，误点一下全没了，页面只轻描淡写
 * 说一句"已新建空白画布"。"打开已存定义"同理。丢的是值班员刚摆好的图，
 * 而问一句的成本远低于重画——所以默认动作是**先确认**，不是先丢再道歉。
 */
export function confirmDiscardUnsaved(what: string, onProceed: () => void): void {
  Modal.confirm({
    title: '画布上有未保存的改动',
    content: `「${what}」会用服务端的内容覆盖当前画布，未保存的节点、连线与摆好的位置都会丢掉。`,
    okText: '丢掉改动，继续',
    cancelText: '取消，先回去保存',
    onOk: onProceed,
  })
}

/**
 * 刷新与关标签页也要拦一次——这才是最容易丢图的那条路。
 *
 * 上面那个确认只管**站内**覆盖；真机量过：画布上摆好 3 个节点、界面上明晃晃写着"未保存"，
 * 按 F5 之后节点归零、浏览器一次确认都没弹（0 次）。也就是说那句"未保存"只提醒了
 * 不刷新的人，对真正会丢图的那一下毫无作用。
 *
 * 浏览器不允许自定义这段提示的文案（安全限制），所以这里能做的就是把"拦得住"这件事做上；
 * 文案的说明仍留在页面里（"未保存"标记 + 保存按钮）。返回解除监听的函数，交给视图卸载时调。
 */
export function installUnsavedGuard(isDirty: () => boolean, target: Window = window): () => void {
  function onBeforeUnload(event: BeforeUnloadEvent): void {
    if (!isDirty()) return
    event.preventDefault()
    // Safari/旧浏览器看的是 returnValue，两个都给才真拦得住
    event.returnValue = ''
  }
  target.addEventListener('beforeunload', onBeforeUnload)
  return () => target.removeEventListener('beforeunload', onBeforeUnload)
}
