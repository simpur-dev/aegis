/**
 * 工作流画布的模型层（Pinia setup store）。
 *
 * 边界：store 只持有"可序列化模型"（WorkflowDef / positions / 实例快照），
 * 不出现 Vue Flow 的 GraphNode 等运行时对象——画布需要的视图由 WorkflowView 用
 * @/utils/graph 的纯函数即时换算，保证任何时刻都能从模型重建视图。
 *
 * 服务端契约里定义列表只返回摘要（workflow_api.py:113-130），不含 nodes/edges，
 * 因此"图结构"只存在于本地草稿：保存 = 创建或修订出新版本，而不是把远端图拉回画布。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import {
  describeDetail,
  toNodeInputs,
  workflowApi,
  type DefinitionSummary,
  type InstanceDetail,
  type InstanceNodeRun,
  type NodeTypeEntry,
  type WorkflowClient,
} from '@/api/workflow'
import {
  assertValidGraph,
  canBypass,
  canDecide,
  canPatchRuntimeConfig,
  defToGraph,
  findNode,
  graphToDef,
  inboundSources,
  isInstanceStateTerminal,
  isInstanceStatus,
  layerPositions,
  layoutLayers,
  patchNode as patchNodeDef,
  removeEdge as removeEdgeFromDef,
  removeNodes,
  setEdgeCondition as setEdgeConditionInDef,
  upsertEdge,
  validateGraph,
  type GraphView,
  type InstanceStatus,
  type JsonValue,
  type NodeDef,
  type NodePatch,
  type NodeState,
  type Position,
  type WorkflowDef,
} from '@/utils/graph'

const POLL_INTERVAL_MS = 1_500

/** 客户端以参数注入：生产用默认实例，测试注入桩适配器，避免真实网络。 */
export function createWorkflowStore(client: WorkflowClient = workflowApi) {
  return defineStore('workflow', () => {
    const nodeTypes = ref<NodeTypeEntry[]>([])
    const definitions = ref<DefinitionSummary[]>([])
    const instances = ref<InstanceDetail[]>([])
    const current = ref<WorkflowDef | null>(null)
    const positions = ref<Record<string, Position>>({})
    const selectedNodeId = ref<string | null>(null)
    const instance = ref<InstanceDetail | null>(null)
    const loading = ref(false)
    const error = ref<string | null>(null)
    const polling = ref(false)
    /**
     * 上一次"画布内容与服务端一致"的快照。
     *
     * 有它才谈得上"未保存"：此前"新建画布"和"打开已存定义"都是直接覆盖画布，
     * 真机上点了三五个节点还没保存，一误触就全没了，页面只轻描淡写说一句"已新建空白画布"。
     * 坐标也算改动——分层布局摆好的位置同样是劳动。
     */
    const savedSnapshot = ref<string | null>(null)
    let timer: ReturnType<typeof setInterval> | null = null

    /**
     * "用户此刻看的是哪一条"的序号：每切一次加一，任何异步回写动手之前先比对。
     *
     * 真机量到两种迟到的回写都会把页面写成两条不同的链路：
     * ① 连开两条定义，先点的那条响应慢了 2 秒，后到的旧答案把画布盖回它身上
     *（标题「雪崩气象型预警与交通管控流程」、11 个节点，而人最后开的是 4 节点那条）；
     * ② 等签实例的一次轮询慢了 2 秒，人在这 2 秒里改点另一条，于是面板实例号是
     * `wfi_ed2670e23f3a`、画布画的是 `wfi_ac42219df5d8` 的那一张，而轮询还在替前者跑。
     * 第二种更要命：接着在画布上点节点提交决策，发出去的是"这条实例 + 那张图的节点号"，
     * 打的是一件谁也没在看的事。
     */
    let viewToken = 0

    function snapshotOf(def: WorkflowDef | null, pos: Record<string, Position>): string {
      return JSON.stringify({ def, pos })
    }

    function markSaved(): void {
      savedSnapshot.value = snapshotOf(current.value, positions.value)
    }

    const isDirty = computed<boolean>(
      () => current.value !== null && snapshotOf(current.value, positions.value) !== savedSnapshot.value,
    )

    const descriptionByType = computed<Record<string, string>>(() =>
      Object.fromEntries(nodeTypes.value.map((entry) => [entry.type, entry.description])),
    )

    const selectedNode = computed<NodeDef | null>(() =>
      current.value === null || selectedNodeId.value === null ? null : findNode(current.value, selectedNodeId.value),
    )

    const validationErrors = computed<string[]>(() =>
      current.value === null ? ['画布尚未初始化'] : validateGraph(current.value),
    )

    /** 本地能拦的先拦下，通过后才提交给服务端二次裁决。 */
    const canSave = computed(() => current.value !== null && validationErrors.value.length === 0)

    const layers = computed<string[][]>(() => {
      const def = current.value
      if (def === null) return []
      try {
        return layoutLayers(
          def.nodes.map((node) => node.node_id),
          def.edges,
        )
      } catch {
        return []
      }
    })

    const runs = computed<Map<string, InstanceNodeRun>>(
      () => new Map((instance.value?.nodes ?? []).map((run) => [run.node_id, run])),
    )

    const instanceStatus = computed<InstanceStatus | null>(() => {
      const raw = instance.value?.status
      return raw !== undefined && isInstanceStatus(raw) ? raw : null
    })

    /** 实例仍可操作 = 未进入终态（model.py:177 TERMINAL）。 */
    const instanceMutable = computed(() => {
      const status = instanceStatus.value
      return status !== null && !isInstanceStateTerminal(status)
    })

    const awaitingNodes = computed<InstanceNodeRun[]>(() =>
      (instance.value?.nodes ?? []).filter((run) => run.state === 'awaiting_human'),
    )

    function nodeStateOf(nodeId: string): NodeState | null {
      return runs.value.get(nodeId)?.state ?? null
    }

    function nodeRun(nodeId: string): InstanceNodeRun | null {
      return runs.value.get(nodeId) ?? null
    }

    /** 运行中操作的可执行判定全部照搬引擎规则，不在前端另立标准。 */
    function canPatchRuntime(nodeId: string): boolean {
      return instanceMutable.value && canPatchRuntimeConfig(nodeStateOf(nodeId))
    }

    function canBypassNode(nodeId: string): boolean {
      return instanceMutable.value && canBypass(nodeStateOf(nodeId))
    }

    function canDecideNode(nodeId: string): boolean {
      return instanceMutable.value && canDecide(nodeStateOf(nodeId))
    }

    /**
     * 决策候选取自定义里的 options（nodes.py:356 声明的白名单），后端 resume 再校验一次。
     * 返回空数组表示"引擎未登记候选"（例如 on_failure=escalate 触发的接管，engine.py:457-461），
     * 此时任何非空 choice 都合法，UI 退化为自由文本输入。
     */
    function decisionOptions(nodeId: string): string[] {
      const def = current.value
      const options = def === null ? undefined : findNode(def, nodeId)?.config.options
      return Array.isArray(options) ? options.filter((item): item is string => typeof item === 'string') : []
    }

    function resetDefinition(name = '未命名防控链路'): void {
      viewToken += 1
      current.value = {
        workflow_id: '',
        name,
        description: '',
        version: 1,
        nodes: [],
        edges: [],
        created_by: 'platform.web',
        status: 'active',
      }
      positions.value = {}
      selectedNodeId.value = null
      error.value = null
      // 空白画布没有"未保存的改动"可言：它本来就什么都没有
      markSaved()
    }

    function autoLayout(): void {
      const def = current.value
      if (def === null) return
      positions.value = layerPositions(
        def.nodes.map((node) => node.node_id),
        def.edges,
      )
    }

    function select(nodeId: string | null): void {
      selectedNodeId.value = nodeId
    }

    /** at 为拖放落点（画布坐标）；省略时沿用分层自动布局或落在最右列下方。 */
    function addNode(node: NodeDef, at?: Position): void {
      const def = current.value
      if (def === null) return
      current.value = { ...def, nodes: [...def.nodes, node] }
      const manual = Object.keys(positions.value).length > 0
      if (at === undefined && !manual) {
        selectedNodeId.value = node.node_id
        return
      }
      // 一旦出现手工坐标就整套固化，避免"半自动半手动"互相压位。
      if (!manual) autoLayout()
      positions.value = { ...positions.value, [node.node_id]: at ?? spawnPosition() }
      selectedNodeId.value = node.node_id
    }

    function spawnPosition(): Position {
      const all = Object.values(positions.value)
      const column = all.length === 0 ? 0 : Math.max(...all.map((position) => position.x))
      const rows = all.filter((position) => position.x === column).map((position) => position.y)
      return { x: column, y: rows.length === 0 ? 0 : Math.max(...rows) + 132 }
    }

    /** 首次手动拖拽前，把当前自动布局坐标固化，避免"半自动半手动"互相压位。 */
    function seedPositions(next: Readonly<Record<string, Position>>): void {
      if (Object.keys(positions.value).length === 0) positions.value = { ...next }
    }

    function removeSelected(): void {
      const def = current.value
      if (def === null || selectedNodeId.value === null) return
      const trimmed = removeNodes(def, [selectedNodeId.value])
      current.value = trimmed
      selectedNodeId.value = trimmed.nodes[0]?.node_id ?? null
    }

    function patchSelected(patch: NodePatch): void {
      if (current.value === null || selectedNodeId.value === null) return
      current.value = patchNodeDef(current.value, selectedNodeId.value, patch)
    }

    function setMeta(meta: { name?: string; description?: string }): void {
      const def = current.value
      if (def === null) return
      current.value = {
        ...def,
        ...(meta.name === undefined ? {} : { name: meta.name }),
        ...(meta.description === undefined ? {} : { description: meta.description }),
      }
    }

    /** 返回 false 表示被本地规则（自环/重复边）挡下。 */
    function connect(source: string, target: string): boolean {
      const def = current.value
      if (def === null || source === target) return false
      const next = upsertEdge(def, source, target, '')
      current.value = next
      return next.edges.length !== def.edges.length
    }

    function updateEdgeCondition(id: string, condition: string): void {
      if (current.value === null) return
      current.value = setEdgeConditionInDef(current.value, id, condition)
    }

    function removeEdge(id: string): void {
      if (current.value === null) return
      current.value = removeEdgeFromDef(current.value, id)
    }

    function moveNode(nodeId: string, position: Position): void {
      positions.value = { ...positions.value, [nodeId]: position }
    }

    /** join/branch/threshold 的 upstream 必须与真实连线一致，否则运行期才报缺上游结果。 */
    function syncUpstreamConfig(nodeId: string): void {
      const def = current.value
      if (def === null) return
      const node = findNode(def, nodeId)
      if (node === null || !('upstream' in node.config)) return
      const sources = inboundSources(def, nodeId)
      const asList = Array.isArray(node.config.upstream)
      current.value = patchNodeDef(def, nodeId, {
        config: { ...node.config, upstream: asList ? sources : (sources[0] ?? '') },
      })
    }

    async function loadNodeTypes(): Promise<void> {
      nodeTypes.value = (await client.nodeTypes()).items
    }

    async function loadDefinitions(): Promise<void> {
      definitions.value = (await client.definitions()).items
    }

    /**
     * 把一份已存的定义取回来放进画布编辑。
     *
     * 这条此前不存在：列表接口只报计数，画布能创建、能保存、能归档，
     * 却打不开任何东西——"流程编排"页因此只能一次性使用，改一版模板要靠重画。
     * 归档按钮就摆在同一行，破坏性的动作有、建设性的没有。
     */
    async function openDefinition(workflowId: string): Promise<boolean> {
      return runAction(async () => {
        const token = ++viewToken
        const def = await client.definition(workflowId)
        if (token !== viewToken) return
        current.value = def
        selectedNodeId.value = def.nodes[0]?.node_id ?? null
        // 后端不存坐标（NodeDef 里没有位置字段）：打开即按分层布局重排。
        // 坐标取自 defToGraph 的结果而不是另算一遍，画布与模型才只有一份布局口径。
        const graph = defToGraph(def, { descriptions: descriptionByType.value })
        positions.value = Object.fromEntries(graph.nodes.map((node) => [node.id, node.position]))
        // 打开的是"定义"不是"实例"：上一张图的轮询必须停掉，否则定时器会继续
        // 改写 current，把刚打开的草稿覆盖成实例快照。
        stopPolling()
        // 画布现在等于服务端那一份，没有未保存改动
        markSaved()
        await Promise.all([loadDefinitions(), loadInstances()])
      }, '打开定义失败')
    }

    async function loadInstances(): Promise<void> {
      instances.value = (await client.instances()).items
    }

    /**
     * 运行中操作改的就是列表上那一行（等人签 → 已停止），做完必须把列表对一遍。
     * 列表拉取失败不能算成"核签失败"：单子已经签出去了，只是没看到新数字，
     * 说反了一句会让人回头重签一次。
     */
    async function syncInstancesQuietly(): Promise<void> {
      try {
        await loadInstances()
      } catch (caught) {
        error.value = `实例列表没刷新：${describeFailure(caught)}`
      }
    }

    /**
     * 动作回的是"那一条实例"的快照，只有人还看着它时才盖到页面上。
     * 切走了还照样盖，就等于把 A 的运行态画在 B 的图上——节点号对不上那张图。
     * 返回是否真的盖了，调用方据此决定要不要顺手停掉轮询。
     */
    function applyInstanceSnapshot(instanceId: string, snapshot: InstanceDetail): boolean {
      if (instance.value?.instance_id !== instanceId) return false
      instance.value = snapshot
      return true
    }

    /**
     * 保存：workflow_id 为空走创建，否则修订出新版本。
     * 传入画布当前视图时以视图为准（graphToDef 逆映射），保证"所见的图即提交的图"。
     */
    async function saveDefinition(canvas?: GraphView): Promise<boolean> {
      const base = current.value
      if (base === null) return false
      const def = canvas === undefined ? base : graphToDef(canvas, base)
      try {
        assertValidGraph(def)
      } catch (caught) {
        error.value = caught instanceof Error ? caught.message : '画布校验未通过'
        return false
      }
      return runAction(async () => {
        const token = viewToken
        const nodes = toNodeInputs(def.nodes)
        const result =
          def.workflow_id === ''
            ? await client.createDefinition({ name: def.name, description: def.description, nodes, edges: def.edges })
            : await client.reviseDefinition(def.workflow_id, { nodes, edges: def.edges, description: def.description })
        // 存这一趟在路上时人已经切去开别的定义/实例，或另起了空白画布：
        // 把刚存的那份盖到别人正在看的那张图上，就是替人改了他眼前的东西。
        if (token !== viewToken) return
        current.value = { ...def, workflow_id: result.workflow_id, name: result.name, version: result.version }
        markSaved()
        await loadDefinitions()
      }, '保存定义失败')
    }

    /** 归档/取消归档改的是服务端那份定义的状态。画布上正开着同一条时把新状态带回来，
     *  否则标题旁继续写"启用中"，而列表里它已经是收不进默认列表的那一版。
     *  状态是服务端改的，不该记在值班员账上（不重打基线就会显示"有未保存改动"、
     *  弹刷新拦截），但只在画布本来就干净时重打——否则等于把人家没存的改动一起抹掉。 */
    function syncCurrentStatus(workflowId: string, status: string): void {
      if (current.value === null || current.value.workflow_id !== workflowId) return
      const wasDirty = isDirty.value
      current.value = { ...current.value, status: status === 'archived' ? 'archived' : 'active' }
      if (!wasDirty) markSaved()
    }

    async function archiveDefinition(workflowId: string): Promise<boolean> {
      return runAction(async () => {
        const result = await client.archiveDefinition(workflowId)
        syncCurrentStatus(workflowId, result.status)
        await loadDefinitions()
      }, '归档定义失败')
    }

    async function restoreDefinition(workflowId: string): Promise<boolean> {
      return runAction(async () => {
        const result = await client.restoreDefinition(workflowId)
        syncCurrentStatus(workflowId, result.status)
        await loadDefinitions()
      }, '取消归档失败')
    }

    async function startInstance(payload: Record<string, JsonValue> = {}): Promise<boolean> {
      const def = current.value
      if (def === null || def.workflow_id === '') {
        error.value = '请先保存工作流定义再启动实例'
        return false
      }
      return runAction(async () => {
        const token = ++viewToken
        const created = await client.startInstance({ workflow_id: def.workflow_id, payload })
        await syncInstancesQuietly()
        if (token !== viewToken) return
        instance.value = created
        startPolling()
      }, '启动实例失败')
    }

    async function focusInstance(instanceId: string): Promise<boolean> {
      stopPolling()
      return runAction(async () => {
        const token = ++viewToken
        const detail = await client.instance(instanceId)
        if (token !== viewToken) return
        instance.value = detail
        /**
         * 把这条实例的定义与运行态一起载入画布。
         *
         * 此前只设 `instance`、不动画布：运行区明明写着"待人工核签：review"，
         * 画布却仍是空白（标题"未命名防控链路"、0 节点）——选不到节点就打不开决策面板，
         * 值班员签不了这张工单，只能回去 curl 接口。指针指到了，路没修。
         */
        const definition = await client.definition(detail.workflow_id)
        if (token !== viewToken) return
        current.value = definition
        const states = Object.fromEntries(
          detail.nodes.map((node) => [node.node_id, { state: node.state, attempts: node.attempts, note: node.error ?? '' }]),
        )
        const graph = defToGraph(definition, { states, descriptions: descriptionByType.value })
        positions.value = Object.fromEntries(graph.nodes.map((node) => [node.id, node.position]))
        const awaiting = detail.nodes.find((node) => node.state === 'awaiting_human') ?? detail.nodes[0]
        selectedNodeId.value = awaiting?.node_id ?? null
        // 聚焦实例载入的是服务端那一份定义 + 由它算出的坐标：同样不算未保存改动
        markSaved()
        startPolling()
      }, '读取实例失败')
    }

    async function refreshInstance(): Promise<void> {
      const instanceId = instance.value?.instance_id
      if (instanceId === undefined) return
      const wasMutable = instanceMutable.value
      const token = viewToken
      try {
        const next = await client.instance(instanceId)
        /**
         * 迟到的轮询不许把面板翻回上一条。
         * 真机量过：等签实例的一次轮询拖 2 秒，人在这 2 秒里改点了另一条，
         * 于是面板实例号是 `wfi_ed2670e23f3a`、画布画的是 `wfi_ac42219df5d8` 那一张，
         * 定时器还接着替不被看着的那条跑下去。
         */
        if (token !== viewToken || instance.value?.instance_id !== instanceId) return
        instance.value = next
        // 轮询发现这条跑完了，顺手把列表对一遍：面板上"等人签 N 张"数的是列表，
        // 而签掉一张的人就在这页上——真机量到签完之后接口归零、小标题还写着 1 张。
        if (wasMutable && isInstanceStatus(next.status) && isInstanceStateTerminal(next.status)) await loadInstances()
      } catch (caught) {
        error.value = describeFailure(caught)
      }
    }

    /** 顶栏那颗"刷新实例"：用户按它是要把这一页的实例信息都对新，不只当前那条。 */
    async function refreshAll(): Promise<boolean> {
      return runAction(async () => {
        await Promise.all([refreshInstance(), loadInstances()])
      }, '刷新实例失败')
    }

    function startPolling(): void {
      if (timer !== null) return
      polling.value = true
      timer = setInterval(() => {
        if (!instanceMutable.value) {
          stopPolling()
          return
        }
        void refreshInstance()
      }, POLL_INTERVAL_MS)
    }

    function stopPolling(): void {
      if (timer !== null) clearInterval(timer)
      timer = null
      polling.value = false
    }

    async function submitDecision(nodeId: string, choice: string, comment = '', by = ''): Promise<boolean> {
      const instanceId = instance.value?.instance_id
      if (instanceId === undefined) return false
      return runAction(async () => {
        // 平台没有登录态：签字人是谁只能由签的人自己写。留空时不发 by 这个键，
        // 让服务端按它的默认记成 unknown——把"值班指挥员"当成默认值等于替人签名。
        const payload = by === '' ? { choice, comment } : { choice, by, comment }
        applyInstanceSnapshot(instanceId, await client.submitDecision(instanceId, nodeId, payload))
        await syncInstancesQuietly()
      }, '提交人工核签失败')
    }

    async function patchRuntimeConfig(nodeId: string, config: Record<string, JsonValue>): Promise<boolean> {
      const instanceId = instance.value?.instance_id
      if (instanceId === undefined) return false
      return runAction(async () => {
        const result = await client.patchNodeConfig(instanceId, nodeId, config)
        // 改参接口只回 {node_id, config}（engine.py:699），需再拉一次实例快照刷新画布状态。
        await refreshInstance()
        /**
         * 引擎按"合并这些键"实现（engine.py:695 `{**node.config, **config}`），
         * 而界面上把一格清空等于把这个键从载荷里删掉——两者一撞，结果是
         * **旧值原地留着、画布显示空、HTTP 200**：值班员以为通知正文清掉了，
         * 实际照旧发出去。删除在这条接口上表达不出来，就至少把它说明白。
         */
        const kept = Object.keys(result.config).filter((key) => !(key in config))
        if (kept.length > 0) {
          error.value = `这 ${kept.length} 格没能改成空：${kept.join('、')}——改参接口按键合并，清空表达不出"删除"，服务端仍是原值；要改请填新值`
        }
      }, '运行中改参失败')
    }

    async function insertRuntimeNode(after: string, node: NodeDef): Promise<boolean> {
      const instanceId = instance.value?.instance_id
      if (instanceId === undefined) return false
      return runAction(async () => {
        await client.insertNode(instanceId, { after, node: toNodeInputs([node])[0] })
        // 插入接口不返回实例快照（engine.py:569），重拉以拿到新节点的运行态。
        applyInstanceSnapshot(instanceId, await client.instance(instanceId))
      }, '插入节点失败')
    }

    async function bypassNode(nodeId: string, reason = ''): Promise<boolean> {
      const instanceId = instance.value?.instance_id
      if (instanceId === undefined) return false
      return runAction(async () => {
        applyInstanceSnapshot(instanceId, await client.bypassNode(instanceId, nodeId, reason))
        await syncInstancesQuietly()
      }, '绕过节点失败')
    }

    async function abortInstance(reason = ''): Promise<boolean> {
      const instanceId = instance.value?.instance_id
      if (instanceId === undefined) return false
      return runAction(async () => {
        if (applyInstanceSnapshot(instanceId, await client.abortInstance(instanceId, reason))) stopPolling()
        await syncInstancesQuietly()
      }, '中止实例失败')
    }

    async function runAction(action: () => Promise<void>, label: string): Promise<boolean> {
      loading.value = true
      error.value = null
      try {
        await action()
        return true
      } catch (caught) {
        error.value = `${label}：${describeFailure(caught)}`
        return false
      } finally {
        loading.value = false
      }
    }

    /**
     * 后端两类错误都要落成人读的一句话：
     * 400 是 WorkflowValidationError 的字符串，422 是 FastAPI 的校验数组。
     *
     * 码与原因之间要有分隔：拼成 `HTTP 422 流程名称（name）：…` 时，
     * 读的人会把"HTTP 422"当成字段名的一部分。
     * 而 `detail` 缺失时不能再自己拼码——客户端构造的那句里已经带了
     * （`接口调用失败（HTTP 0）：timeout…`），再拼一次就是两个前缀。
     */
    function describeFailure(caught: unknown): string {
      if (caught instanceof Error && 'status' in caught) {
        const wrapped = caught as Error & { status: number; detail?: unknown }
        const reason = describeDetail(wrapped.detail)
        return reason === '' ? wrapped.message : `HTTP ${wrapped.status}：${reason}`
      }
      return caught instanceof Error ? caught.message : '未知错误'
    }

    return {
      nodeTypes,
      definitions,
      instances,
      current,
      positions,
      selectedNodeId,
      instance,
      loading,
      error,
      polling,
      isDirty,
      descriptionByType,
      selectedNode,
      validationErrors,
      canSave,
      layers,
      instanceStatus,
      instanceMutable,
      awaitingNodes,
      nodeStateOf,
      nodeRun,
      canPatchRuntime,
      canBypassNode,
      canDecideNode,
      decisionOptions,
      resetDefinition,
      autoLayout,
      select,
      addNode,
      spawnPosition,
      seedPositions,
      removeSelected,
      patchSelected,
      setMeta,
      connect,
      updateEdgeCondition,
      removeEdge,
      moveNode,
      syncUpstreamConfig,
      loadNodeTypes,
      loadDefinitions,
      openDefinition,
      loadInstances,
      saveDefinition,
      archiveDefinition,
      restoreDefinition,
      startInstance,
      focusInstance,
      refreshInstance,
      refreshAll,
      startPolling,
      stopPolling,
      submitDecision,
      patchRuntimeConfig,
      insertRuntimeNode,
      bypassNode,
      abortInstance,
      describeFailure,
    }
  })
}

export const useWorkflowStore = createWorkflowStore()

export type WorkflowStore = ReturnType<typeof useWorkflowStore>
