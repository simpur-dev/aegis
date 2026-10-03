import { createApp } from 'vue'
import Antd from 'ant-design-vue'
import { createPinia } from 'pinia'
import 'ant-design-vue/dist/reset.css'

import App from './App.vue'
import { router } from './router'

const app = createApp(App)
app.use(Antd)
// pinia 必须在这里装上：`stores/workflow.ts` 被六个画布组件在 setup 顶层取用，
// 缺这一句时 /workflow 整页渲染成空白，而控制台只有一条 Vue 的组件栈 warn——
// 单测全都自带 `createPinia()` 挂载，所以 400+ 项全绿也照不出这个洞（真机巡检实测）。
app.use(createPinia())
app.use(router)

/**
 * 视图抛异常时，Vue 默认只打一条 "[Vue warn]: Unhandled error during execution of setup function"
 * 加一串组件栈，**异常对象本身不进控制台**（实测：崩掉的那次控制台里 211 条 warn、0 条 error）。
 * 后果是页面整块空白而现场查不到原因——运维只知道"页面没了"。这里把真异常打出来，
 * 并把出错位置一起带上；不吞掉，也不改成静默降级。
 */
app.config.errorHandler = (error, _instance, info) => {
  const where = String(info ?? 'unknown')
  if (error instanceof Error) console.error(`[aegis] 视图异常（${where}）：${error.message}`, { stack: error.stack })
  else console.error(`[aegis] 视图异常（${where}）：`, error)
}

app.mount('#app')
