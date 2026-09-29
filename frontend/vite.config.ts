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
      '/metrics': { target: apiTarget, changeOrigin: true },
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
  },
})
