import { Modal } from 'ant-design-vue'

/**
 * 归档之前先问一句。
 *
 * 真机上这一按立刻生效：把内置的「人工上报核签流程」归档之后，本班次每一条低置信度上报
 * 都只回一句"没开出工单"，而注册只在服务启动时跑一次，运行中不会自己补回来。
 * 定义列表原先只有"打开"和"归档"两个按钮，连撤销的入口都没有——
 * 所以这一按既要有确认，也要留得下退路（配套的「取消归档」在同一行）。
 */
export function confirmArchive(name: string, version: number, onProceed: () => void): void {
  Modal.confirm({
    title: `归档「${name}」v${version}？`,
    content:
      '归档立刻生效：按名字自动取用这条定义的链路（低置信度上报开核签工单）会停发，' +
      '上报那边只会多一句"没有开出工单"，运行中的服务不会自己补注册。' +
      '这一行会留下「取消归档」，随时撤回。',
    okText: '归档',
    cancelText: '取消',
    onOk: onProceed,
  })
}
