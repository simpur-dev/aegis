#!/usr/bin/env node
/**
 * 本地开发一键启动：Docker 基础设施 + 本地后端 + 前端。
 *
 * 这个脚本的立场只有一条：**全系统只有一份数据**。
 *
 * - 容器里跑 nats / postgres / neo4j（`deploy/docker-compose.yml` 的默认档），数据都在 Docker 卷里；
 * - 后端是本机 uv 起的进程（改 Python 不用重建镜像），但 `AEGIS_STORE_BACKEND=postgres`，
 *   DSN 指向容器那个库——不是 memory；知识层也连容器里的 neo4j；
 * - 前端是 vite（5173），`/api` 代理到本机后端。
 *
 * 为了让"一份数据"成立，起本地后端之前会先停掉容器里的 `aegis-backend`：否则两个后端
 * 同时写同一个 postgres、各自跑一份模拟器，数据会互相踩。`aegis-frontend`（8080）的 /api
 * 指向的是容器后端，留着只会 502，也一并停掉。
 *
 * 为什么不再用 memory 形态：`npm run dev` 和 Docker 栈各存一份数据，用到最后没人说得清
 * 屏幕上这条预警在哪个库里——2026-10-24 就是这么乱的。要纯离线跑后端仍然可以手工
 * `cd backend && uv run python -m aegis.main`（`backend/.env` 里就是 memory 形态）。
 *
 * 用法：
 *   npm run dev                 # 容器 + 后端 + 前端，一条命令
 *   npm run dev:backend         # 只起容器与后端
 *   npm run dev:frontend        # 只起前端（代理指向同一个端口）
 *   AEGIS_DEV_PORT=8321 npm run dev
 *   AEGIS_API_TARGET=http://127.0.0.1:8000 npm run dev:frontend   # 显式指到容器后端
 */
import { spawn, spawnSync } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import net from 'node:net'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import concurrently from 'concurrently'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const backendDir = resolve(root, 'backend')
const frontendDir = resolve(root, 'frontend')
const composeFile = resolve(root, 'deploy', 'docker-compose.yml')
const composeEnvFile = resolve(root, '.env')

const sleep = (ms) => new Promise((done) => setTimeout(done, ms))

function die(message) {
  console.error(`\n[aegis-dev] ${message}\n`)
  process.exit(1)
}

const onlyArg = process.argv.slice(2).find((arg) => arg.startsWith('--only='))
const only = onlyArg ? onlyArg.slice('--only='.length) : undefined
if (only !== undefined && only !== 'backend' && only !== 'frontend') {
  die(`--only 只接受 backend 或 frontend，收到的是 ${only}`)
}

const startBackend = only !== 'frontend'
const startFrontend = only !== 'backend'

const port = Number(process.env.AEGIS_DEV_PORT ?? 8001)
if (!Number.isInteger(port) || port < 1 || port > 65535) {
  die(`AEGIS_DEV_PORT 不是合法端口：${process.env.AEGIS_DEV_PORT}`)
}

// 显式给了 AEGIS_API_TARGET 就尊重它（比如把前端指到 Docker 里那个后端）
const apiTarget = process.env.AEGIS_API_TARGET ?? `http://127.0.0.1:${port}`

function isListening(host, targetPort) {
  return new Promise((done) => {
    const socket = net.connect({ host, port: targetPort })
    const finish = (listening) => {
      socket.destroy()
      done(listening)
    }
    socket.setTimeout(500)
    socket.once('connect', () => finish(true))
    socket.once('timeout', () => finish(false))
    socket.once('error', () => finish(false))
  })
}

async function assertPortFree() {
  const [v4, v6] = await Promise.all([isListening('127.0.0.1', port), isListening('::1', port)])
  if (v4 || v6) {
    die(
      `端口 ${port} 已被占用，本地后端起不来。\n` +
        `  · 换一个端口：AEGIS_DEV_PORT=8321 npm run dev\n` +
        `  · 或者先关掉占着 ${port} 的那个进程再跑`,
    )
  }
}

/* ------------------------------------------------------------------ Docker */

function dockerCliAvailable() {
  const result = spawnSync('docker', ['--version'], { stdio: 'ignore' })
  return !result.error && result.status === 0
}

function dockerEngineReady() {
  const result = spawnSync('docker', ['info', '--format', '{{.ServerVersion}}'], { stdio: 'ignore' })
  return !result.error && result.status === 0
}

function dockerDesktopLauncher() {
  const candidates = [
    process.env.LOCALAPPDATA ? resolve(process.env.LOCALAPPDATA, 'Programs', 'DockerDesktop', 'Docker Desktop.exe') : '',
    'C:\\Program Files\\Docker\\Docker\\Docker Desktop.exe',
    'D:\\Docker\\DockerDesktop\\Docker Desktop.exe',
  ]
  return candidates.find((candidate) => candidate && existsSync(candidate))
}

async function ensureDockerEngine() {
  if (!dockerCliAvailable()) {
    die('没找到 docker 命令：先装 Docker Desktop，或者把 docker 放进 PATH 再跑这条命令')
  }
  if (dockerEngineReady()) return

  const launcher = dockerDesktopLauncher()
  if (process.platform !== 'win32' || !launcher) {
    die(
      'Docker 引擎没在跑。这条命令要用容器里的 postgres/neo4j，先手动启动 Docker 再试。\n' +
        (dockerDesktopLauncher() ? '' : '  （也没找到 Docker Desktop 的启动器）'),
    )
  }

  console.log('[dev] Docker 引擎没在跑，正在拉起 Docker Desktop …')
  spawn(launcher, [], { detached: true, stdio: 'ignore' }).unref()

  const deadline = Date.now() + 240_000
  let waited = 0
  while (Date.now() < deadline) {
    await sleep(3000)
    waited += 3
    if (dockerEngineReady()) {
      console.log(`[dev] Docker 引擎就绪（等了 ${waited}s）`)
      return
    }
  }
  die('等了 4 分钟 Docker 引擎还没起来：打开 Docker Desktop 看一眼它的报错，修好再跑')
}

function composeArgs(...args) {
  return ['compose', '--env-file', composeEnvFile, '-f', composeFile, ...args]
}

async function composeUpWithRetry() {
  console.log('[dev] 启动基础设施容器（nats / postgres / neo4j）…')
  let lastStatus = 0
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    const result = spawnSync('docker', composeArgs('up', '-d'), { stdio: 'inherit' })
    if (!result.error && result.status === 0) return
    lastStatus = result.status ?? 1
    if (attempt < 3) {
      console.log(`[dev] compose up 第 ${attempt} 次失败（exit ${lastStatus}），5 秒后重试…`)
      await sleep(5000)
    }
  }
  die(`docker compose up 连续失败（exit ${lastStatus}）：看上面的输出，先修好容器再跑`)
}

async function waitForPostgres() {
  console.log('[dev] 等 postgres 健康检查通过 …')
  for (let attempt = 0; attempt < 60; attempt += 1) {
    const result = spawnSync('docker', ['inspect', '--format', '{{.State.Health.Status}}', 'aegis-postgres'], {
      encoding: 'utf8',
    })
    if (!result.error && result.status === 0 && result.stdout.trim() === 'healthy') return
    await sleep(2000)
  }
  die('postgres 容器 2 分钟没等到 healthy：docker logs aegis-postgres 看看卡在哪')
}

function containerRunning(name) {
  const result = spawnSync('docker', ['ps', '--filter', `name=^/${name}$`, '--format', '{{.Names}}'], {
    encoding: 'utf8',
  })
  return !result.error && result.status === 0 && result.stdout.trim() === name
}

function stopContainer(name, why) {
  if (!containerRunning(name)) return false
  const result = spawnSync('docker', ['stop', name], { stdio: 'inherit' })
  if (result.error || result.status !== 0) {
    die(`停不掉容器 ${name}：两个后端写同一个库比这更糟，先手动 docker stop ${name} 再跑`)
  }
  console.log(`[dev] 已停容器 ${name}（${why}）`)
  return true
}

/* ------------------------------------------------------- 配置与后端环境变量 */

function loadDotEnv(file) {
  const values = {}
  if (!existsSync(file)) return values
  for (const rawLine of readFileSync(file, 'utf8').split(/\r?\n/)) {
    const line = rawLine.trim()
    if (!line || line.startsWith('#')) continue
    const eq = line.indexOf('=')
    if (eq <= 0) continue
    const key = line.slice(0, eq).trim()
    let value = line.slice(eq + 1).trim()
    const quoted =
      value.length >= 2 &&
      ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'")))
    if (quoted) {
      value = value.slice(1, -1)
    } else {
      // 与 compose 的 dotenv 口径一致：未加引号时，空白后的 # 起是注释
      value = value.replace(/\s+#.*$/, '').trim()
    }
    values[key] = value
  }
  return values
}

function postgresEnv(dotenv) {
  const user = dotenv.POSTGRES_USER || 'aegis'
  const database = dotenv.POSTGRES_DB || 'aegis'
  const password = dotenv.POSTGRES_PASSWORD
  if (!password) {
    die('仓库根 .env 里没有 POSTGRES_PASSWORD：postgres 容器起得来，但本地后端连不上库')
  }
  const dsn = `postgresql://${user}:${encodeURIComponent(password)}@127.0.0.1:5432/${database}`
  return {
    AEGIS_STORE_BACKEND: 'postgres',
    AEGIS_PG_DSN: dsn,
    AEGIS_PG_APPLY_MIGRATIONS_ON_START: 'true',
  }
}

function knowledgeEnv(dotenv) {
  if (!dotenv.NEO4J_PASSWORD) return {}
  return {
    AEGIS_KNOWLEDGE_GRAPHITI_URI: 'bolt://127.0.0.1:7687',
    AEGIS_NEO4J_USER: dotenv.NEO4J_USERNAME || 'neo4j',
    AEGIS_NEO4J_PASSWORD: dotenv.NEO4J_PASSWORD,
  }
}

function retrievalEnv() {
  const models = resolve(backendDir, 'data', 'models')
  if (!existsSync(models)) {
    console.warn('[dev] backend/data/models 不存在：retrieval 保持关闭（要开先跑 python scripts/fetch_retrieval_models.py）')
    return {}
  }
  return { AEGIS_RETRIEVAL_ENABLED: 'true', AEGIS_RETRIEVAL_MODEL_DIR: './data/models' }
}

function tracingEnv() {
  // observability profile 起着 jaeger 时把本地跨度也送过去，证据链和容器版一致
  return containerRunning('aegis-jaeger')
    ? { OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: 'http://127.0.0.1:4318/v1/traces' }
    : {}
}

function backendCommand() {
  const uv = spawnSync('uv', ['--version'], { stdio: 'ignore' })
  if (uv.status === 0) return 'uv run python -m aegis.main'
  // uv 不在 PATH 时退回项目自己的虚拟环境，保证"有 venv 就能起"
  const venvPython = resolve(backendDir, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
  if (existsSync(venvPython)) return `"${venvPython}" -m aegis.main`
  die(
    '既没找到 uv，也没有 backend/.venv。先按 README §2.1 装后端依赖：\n' +
      '  cd backend && uv sync --extra dev --extra postgres --extra graph',
  )
}

/* --------------------------------------------------------------------- 主流程 */

if (!existsSync(composeFile)) die(`找不到 ${composeFile}，仓库结构不对`)

// 先把能当场判死的都判完，再动 Docker：否则半路报错会把容器后端停了却起不来本地后端
const dotenv = startBackend ? loadDotEnv(composeEnvFile) : {}
const backendBaseEnv = startBackend
  ? { ...postgresEnv(dotenv), ...knowledgeEnv(dotenv), ...retrievalEnv() }
  : {}
const backendCmd = startBackend ? backendCommand() : ''

if (startFrontend && !existsSync(resolve(frontendDir, 'node_modules'))) {
  die('frontend/node_modules 不存在，先装前端依赖：\n  cd frontend && npm install')
}

const stopped = []

if (startBackend) {
  await assertPortFree()

  await ensureDockerEngine()
  await composeUpWithRetry()
  await waitForPostgres()

  // 一份数据的前提：容器后端必须让位给本地后端
  if (stopContainer('aegis-backend', '本地后端接管，两个后端写同一个库会互相踩')) stopped.push('aegis-backend')
  if (stopContainer('aegis-frontend', '它的 /api 指向容器后端，留着只会 502')) stopped.push('aegis-frontend')
}

const commands = []

if (startBackend) {
  commands.push({
    name: 'backend',
    command: backendCmd,
    cwd: backendDir,
    prefixColor: 'cyan',
    env: {
      AEGIS_HTTP_HOST: '127.0.0.1',
      AEGIS_HTTP_PORT: String(port),
      ...backendBaseEnv,
      ...tracingEnv(),
    },
  })
}

if (startFrontend) {
  commands.push({
    name: 'frontend',
    command: 'npm run dev',
    cwd: frontendDir,
    prefixColor: 'magenta',
    env: { AEGIS_API_TARGET: apiTarget },
  })
}

console.log('')
console.log('  AEGIS 本地开发（一份数据：容器里的 postgres）')
if (startBackend) {
  console.log(`  容器       nats / postgres / neo4j 常驻；Ctrl+C 只停本地进程`)
  console.log(`  后端 API   http://127.0.0.1:${port}   store=postgres（与容器同一份库）`)
}
if (startFrontend) {
  console.log(`  前端 Web   http://localhost:5173   （vite 代理 /api → ${apiTarget}）`)
}
if (stopped.length) {
  console.log(`  已让位     ${stopped.join(' / ')}（避免两个后端写同一份数据）`)
}
console.log('  收工       Ctrl+C 一次同时停本地两端，容器保持运行')
console.log('')

const { result } = concurrently(commands, {
  prefix: '[{name}]',
  padPrefix: true,
  killOthersOn: ['failure'],
  killTimeout: 5000,
})

function shutdown(code) {
  console.log('')
  console.log('[dev] 本地两端已停；Docker 容器保持运行，数据都在 postgres/neo4j 卷里。')
  if (stopped.length) {
    console.log(`[dev] 要回到"全容器"形态：docker start ${stopped.join(' ')}`)
  }
  process.exit(code)
}

result.then(
  () => shutdown(0),
  () => shutdown(1),
)
