/**
 * 用例用的本机静态资源服务：只监听 127.0.0.1、只挂 `frontend/public`，并把每一次请求记下来。
 *
 * 为什么抽成一份而不是各用例自己写：`offline-assets.spec.ts`（真读取器读字节）与
 * `terrain-parse.spec.ts`（真 Cesium 解析瓦片）必须打同一套 HTTP 语义。两遍各自实现的
 * Range / 缺件兜底迟早会漂，漂了以后一条用例红、另一条仍然绿，就看不出是资产坏了。
 *
 * 为什么要能选"缺件怎么答"这一档：浏览器里 `vite preview` 对不存在的瓦回的是
 * `200 text/html`（实测），而不是干净的 404。Cesium 会把那 585 字节 HTML 当二进制硬解，
 * 报 `RangeError: Invalid typed array length`。真实部署里"资产没烘上"就是这个回答形状，
 * 门禁必须能复现它，否则测的是理想世界。
 */

import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http'
import { existsSync, type Stats } from 'node:fs'
import { readFile, stat } from 'node:fs/promises'
import { extname, join, resolve } from 'node:path'

/** 缺件时的应答：`not-found` 是老实的 404，`spa-html` 是 `vite preview` 的 SPA 兜底页。 */
export type MissingAssetResponse = 'not-found' | 'spa-html'

const CONTENT_TYPES: Record<string, string> = {
  '.json': 'application/json',
  '.terrain': 'application/vnd.quantized-mesh',
  '.pmtiles': 'application/vnd.pmtiles',
  '.png': 'image/png',
}

/** SPA 兜底页刻意做成 585 字节量级：与真 `vite preview` 同量级，才不至于让"字节太少"掩盖解析错误。 */
const SPA_HTML_BODY = `<!doctype html><html><head><meta charset="utf-8"><title>AEGIS</title></head><body><div id="app">${'a'.repeat(480)}</div></body></html>`

export interface AssetRequest {
  readonly url: string
  readonly method: string
  readonly host: string
  readonly accept: string | null
  readonly range: string | null
}

export interface LocalAssetServer {
  readonly origin: string
  /** 按发生顺序记录的请求；用例自己决定要不要清空再断言。 */
  readonly requests: AssetRequest[]
  close(): Promise<void>
}

/**
 * 从 vitest 的工作目录往上找带烘焙资产的 `public/`。
 * 锚点用 `terrain/layer.json`：地形与底图两条腿由同一次烘焙产出，任缺其一都该响亮失败，
 * 所以这里宁可比"路径不存在"更具体地把命令写出来，也不要让用例对着空目录拿到一串假绿。
 */
export function publicAssetDir(): string {
  let dir = process.cwd()
  for (let depth = 0; depth < 5; depth += 1) {
    const candidate = resolve(dir, 'public')
    if (existsSync(join(candidate, 'terrain', 'layer.json')) && existsSync(join(candidate, 'basemaps', 'aegis.pmtiles'))) {
      return candidate
    }
    dir = resolve(dir, '..')
  }
  throw new Error(
    `没找到同时含 terrain/layer.json 与 basemaps/aegis.pmtiles 的 public/（cwd=${process.cwd()}）；先跑 python scripts/build_offline_tiles.py`,
  )
}

export async function startLocalAssetServer(
  options: { missing?: MissingAssetResponse; publicDir?: string } = {},
): Promise<LocalAssetServer> {
  const publicDir = options.publicDir ?? publicAssetDir()
  const missing = options.missing ?? 'not-found'
  const requests: AssetRequest[] = []
  let origin = 'http://127.0.0.1'

  const server: Server = createServer((request, response) => {
    const url = new URL(request.url ?? '/', origin)
    requests.push({
      url: url.pathname,
      method: request.method ?? 'GET',
      host: url.host,
      accept: request.headers.accept ?? null,
      range: request.headers.range ?? null,
    })
    const relative = decodeURIComponent(url.pathname).replace(/^\/+/, '')
    void serve(relative, request, response)
  })

  async function serve(relative: string, request: IncomingMessage, response: ServerResponse): Promise<void> {
    let info: Stats
    try {
      info = await stat(join(publicDir, relative))
    } catch {
      answerMissing(response, request, missing)
      return
    }
    if (!info.isFile()) {
      response.writeHead(403).end()
      return
    }
    const body = await readFile(join(publicDir, relative))
    const headers = {
      'Content-Type': CONTENT_TYPES[extname(relative)] ?? 'application/octet-stream',
      'Accept-Ranges': 'bytes',
    }
    const range = request.headers.range
    const matched = range ? /^bytes=(\d+)-(\d*)$/.exec(range) : null
    if (range && !matched) {
      //  Range 语法不认就回 416 并告知全长：PMTiles 的随机读靠这个头判断自己算错了没有。
      response.writeHead(416, { ...headers, 'Content-Range': `bytes */${info.size}` }).end()
      return
    }
    if (matched) {
      const start = Number(matched[1])
      const end = matched[2] ? Math.min(Number(matched[2]), info.size - 1) : Math.min(start + 65535, info.size - 1)
      response.writeHead(206, {
        ...headers,
        'Content-Range': `bytes ${start}-${end}/${info.size}`,
        'Content-Length': String(end - start + 1),
      })
      response.end(request.method === 'HEAD' ? undefined : body.subarray(start, end + 1))
      return
    }
    response.writeHead(200, { ...headers, 'Content-Length': String(body.byteLength) })
    response.end(request.method === 'HEAD' ? undefined : body)
  }

  await new Promise<void>((done) => server.listen(0, '127.0.0.1', done))
  const address = server.address()
  if (address === null || typeof address === 'string') throw new Error('本机静态服务没拿到端口')
  origin = `http://127.0.0.1:${address.port}`

  return {
    origin,
    requests,
    close: () => new Promise<void>((done, fail) => server.close((error) => (error ? fail(error) : done()))),
  }
}

function answerMissing(response: ServerResponse, request: IncomingMessage, missing: MissingAssetResponse): void {
  if (missing === 'spa-html') {
    response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' })
    response.end(request.method === 'HEAD' ? undefined : SPA_HTML_BODY)
    return
  }
  response.writeHead(404).end()
}
