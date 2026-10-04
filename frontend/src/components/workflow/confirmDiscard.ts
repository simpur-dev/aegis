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
