/** 右键菜单与节点定位共用的条目形状；`<script setup>` 不能导出类型，所以单独放这里。 */
export interface ContextMenuItem {
  id: string
  label: string
  shortcut?: string
  disabled?: boolean
  danger?: boolean
  separator?: boolean
}

export interface LocatorNode {
  id: string
  name: string
  type: string
}
