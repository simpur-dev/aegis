import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const apiTarget = process.env.AEGIS_API_TARGET ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: apiTarget, changeOrigin: true },
      // `/metrics` 有两个主人：后端的 Prometheus 抓取路径，与前端"指标量测"页的路由。
      // 一律转发会让 dev 下点开这个页面永远拿回一段文本而不是应用（真机实测踩过），
      // 按 Accept 分流：浏览器导航带 text/html 就交给 SPA，抓取端不带就转后端。
      '/metrics': {
        target: apiTarget,
        changeOrigin: true,
        bypass: (req) => (req.headers.accept?.includes('text/html') ? '/index.html' : undefined),
      },
      '/healthz': { target: apiTarget, changeOrigin: true },
      '/readyz': { target: apiTarget, changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        // 重型依赖独立分包：业务代码改动不至于让用户重新下载 UI/图表库
        manualChunks: {
          vendor: ['vue', 'vue-router', 'axios'],
          ui: ['ant-design-vue'],
          charts: ['echarts'],
        },
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['src/**/*.spec.ts'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'json-summary'],
      include: [
        'src/utils/graph/**/*.ts',
        'src/composables/workflow/**/*.ts',
        'src/stores/workflow/**/*.ts',
        'src/api/workflow/codec.ts',
      ],
      exclude: ['**/__tests__/**', '**/__fixtures__/**', '**/*.spec.ts'],
      thresholds: {
        lines: 90,
        functions: 90,
        statements: 90,
        branches: 85,
      },
    },
  },
})
