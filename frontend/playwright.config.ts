import { defineConfig, devices } from '@playwright/test'

/**
 * 视觉烟雾门禁的配置。刻意只跑本机 chromium：本项目的 P0 口径是"弱网离线可用"，
 * 任何云测真机/第三方 SaaS 都要账号与外网，拿它的绿来当交付证据是反的。
 *
 * 跑的是**构建产物**（`npm run build` → `vite preview`）而不是 dev server：
 * P0 要交的是"构建一次、离线可用"，开发服务器的模块图与 dist/ 不是同一份东西，
 * 只在 dev 下绿过不等于现场绿。
 */

const PORT = Number(process.env.AEGIS_E2E_PORT ?? 4175)
const baseURL = `http://127.0.0.1:${PORT}`

export default defineConfig({
  testDir: './e2e',
  outputDir: 'test-results/e2e',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 180_000,
  reporter: [['list']],
  use: {
    baseURL,
    trace: 'off',
    video: 'off',
    screenshot: 'only-on-failure',
    viewport: { width: 1280, height: 800 },
    launchOptions: {
      // headless 里没有真 GPU：让 ANGLE 走 SwiftShader，否则 WebGL 上下文直接创建失败，
      // Cesium 会连画布都不出——那会被误读成"资产坏了"。
      args: ['--ignore-gpu-blocklist', '--enable-unsafe-swiftshader', '--use-gl=angle', '--use-angle=swiftshader'],
    },
  },
  webServer: {
    // `--host 127.0.0.1` 不是多余的：vite preview 默认只绑 `[::1]`（实测 netstat），
    // baseURL 用 127.0.0.1 时探活永远连不上，整条门禁会卡在 "Timed out waiting from config.webServer"。
    command: `npm run build && npx vite preview --host 127.0.0.1 --port ${PORT} --strictPort`,
    url: baseURL,
    timeout: 600_000,
    reuseExistingServer: !process.env.CI,
    stdout: 'ignore',
    stderr: 'pipe',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
})
