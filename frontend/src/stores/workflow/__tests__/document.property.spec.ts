import fc from 'fast-check'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { decodeWorkflowDocument, encodeDocument } from '@/api/workflow/codec'
import { NODE_SPECS, defToGraph, graphToDef, NODE_TYPES, type JsonValue, type NodeType } from '@/utils/graph'
import { definitionFromDocument, documentsEqual, emptyLayout, type WorkflowDocument } from '@/utils/graph/document'

import { createDocumentStore, type DocumentStore } from '../document'
import { LINEAGE, draftOf, gatewayStub, nodeDef, versionSummary, workflowDocument } from './fixtures'
import type { DraftPutInput, WorkflowGateway } from '@/api/workflow/contract'

/**
 * 批次 D4 的性质测试：**19 类节点"编辑 → 保存 → 重开"必须逐字段无损**。
 *
 * 为什么用性质测试而不是给 19 类各写一条用例：无损这件事的破法取决于**值长什么样**
 * （嵌套对象、数组里的 null、中文键、`-0`、极大数、`__proto__` 当值…），列举永远漏一支。
 * 真踩过的两类也都是这种：`extra=forbid` 把多出来的键吃掉、以及"取出来原样存回去"时
 * 某格被映射成 undefined 而消失。
 *
 * 一趟走三层，缺一层就少一类证据：
 *  1. 编解码：`encodeDocument` → 过一次真 JSON（服务端看到的形状）→ `decodeWorkflowDocument`；
 *  2. store：编辑 → `saveNow`（带 revision 的 PUT，假服务端也过一次 JSON）→ 换一台 store `openLineage` 读回；
 *  3. 画布：`defToGraph` → `graphToDef` 的往返（视图重建等于没动过）。
 */

/** 递归 JSON 值（两层）：不用 `fc.jsonValue`，因为要把 `__proto__` 这类名字当普通值/键生成出来。 */
const leaf: fc.Arbitrary<JsonValue> = fc.oneof(
  fc.constant(null),
  fc.boolean(),
  fc.integer({ min: -1000, max: 1000 }).map((value) => value / 7),
  fc.string(),
  fc.constant('__proto__'),
)
const arrayValue = fc.array(leaf, { maxLength: 3 })
const objectValue = fc.dictionary(fc.string(), fc.oneof(leaf, arrayValue), { maxKeys: 3 })
const jsonValue: fc.Arbitrary<JsonValue> = fc.oneof(leaf, arrayValue, objectValue)

/**
 * 按该类型后端白名单里真有的那几格造 config：既覆盖"填了的格"，也覆盖"整格缺失"。
 *
 * 这里取 `NODE_SPECS` 而不是面板的 `NODE_META`：store 层不许依赖组件层（分层守卫），
 * 而"面板字段 = 白名单"这件事由 `components/workflow/__tests__/nodeForms.spec.ts` 按集合相等钉住。
 */
function configArbitrary(type: NodeType): fc.Arbitrary<Record<string, JsonValue>> {
  const keys = [...NODE_SPECS[type].required_config, ...NODE_SPECS[type].optional_config]
  if (keys.length === 0) return fc.constant({})
  const pairs = fc.array(fc.tuple(fc.constantFrom(...keys), jsonValue), { maxLength: keys.length })
  return pairs.map((entries) => Object.fromEntries(entries))
}

function singleNodeDocument(type: NodeType, config: Record<string, JsonValue>): WorkflowDocument {
  return workflowDocument({
    nodes: [nodeDef('n_1', type, config)],
    edges: [],
    layout: { ...emptyLayout(), positions: { n_1: { x: 0, y: 0 } } },
  })
}

/** 过一次真 JSON：假服务端不许偷偷把域对象原样递回来。 */
function throughWire(document: WorkflowDocument): WorkflowDocument {
  const wire = JSON.parse(JSON.stringify(encodeDocument(document))) as unknown
  return decodeWorkflowDocument(wire)
}

let stores: DocumentStore[] = []

function bootstrap(gateway: ReturnType<typeof gatewayStub>): DocumentStore {
  const store = createDocumentStore(gateway)()
  stores.push(store)
  return store
}

beforeEach(() => {
  setActivePinia(createPinia())
  stores = []
})

afterEach(() => {
  for (const store of stores) store.dispose()
  stores = []
})

describe('19 类节点编辑-保存-重开无损（性质）', () => {
  it('编解码一趟过真 JSON：任意 config 逐字段回来', () => {
    for (const type of NODE_TYPES) {
      fc.assert(
        fc.property(configArbitrary(type), (config) => {
          const document = singleNodeDocument(type, config)
          const reopened = throughWire(document)
          expect(documentsEqual(reopened, document), `${type} 编解码后不相等`).toBe(true)
          expect(reopened.nodes[0]?.config).toEqual(config)
        }),
        { numRuns: 60 },
      )
    }
  })

  it('store 层：编辑 → saveNow → 换一台 store 读回，与画布上那份相等', async () => {
    for (const type of NODE_TYPES) {
      await fc.assert(
        fc.asyncProperty(configArbitrary(type), fc.string({ minLength: 2, maxLength: 20 }), async (config, name) => {
          let server = singleNodeDocument(type, {})
          let revision = 2
          const gateway = gatewayStub({
            getDraft: vi.fn<WorkflowGateway['getDraft']>(async () => draftOf(server, { revision })),
            putDraft: vi.fn<WorkflowGateway['putDraft']>(async (_lineage: string, payload: DraftPutInput) => {
              server = throughWire(payload.document)
              revision = payload.base_revision + 1
              return draftOf(server, { revision })
            }),
            listVersions: vi.fn<WorkflowGateway['listVersions']>(async () => [versionSummary()]),
          })

          const editor = bootstrap(gateway)
          expect(await editor.openLineage(LINEAGE)).toBe(true)
          editor.patchNode('n_1', { config, name })
          const edited = editor.document
          if (edited === null) throw new Error('编辑后文档不见了')
          await editor.saveNow()

          // "重开"= 换一个 store 从同一台服务端再读一次：画布上那份对象全部作废。
          const reopened = bootstrap(gateway)
          expect(await reopened.openLineage(LINEAGE)).toBe(true)
          expect(reopened.definition?.nodes[0]?.config).toEqual(config)
          // 裁首尾空格是"提交前"那一步做的事（graphToDef），草稿写入不裁——这里按写进去的原样判。
          expect(reopened.definition?.nodes[0]?.name).toBe(edited.nodes[0]?.name)
          if (reopened.document === null) throw new Error('重开后文档不见了')
          expect(documentsEqual(reopened.document, edited)).toBe(true)
        }),
        { numRuns: 12 },
      )
    }
  })

  it('画布往返：defToGraph → graphToDef 与原文档逐字段相等', () => {
    for (const type of NODE_TYPES) {
      fc.assert(
        fc.property(configArbitrary(type), (config) => {
          // 视图重建走的是"定义"那一侧的形状（workflow_id/version/…），与文档层差一份身份字段。
          const definition = definitionFromDocument(throughWire(singleNodeDocument(type, config)))
          expect(graphToDef(defToGraph(definition), definition)).toEqual(definition)
        }),
        { numRuns: 40 },
      )
    }
  })
})
