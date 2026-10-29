import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 开发模式（npm run dev）将请求代理到 8770 端口的后端；
// 生产模式由后端直接提供 dist 目录中的静态文件。
export default defineConfig({
  plugins: [react()],
  base: './',
  server: {
    port: 5174,
    proxy: {
      '/api': 'http://127.0.0.1:8770',
      '/ws': { target: 'ws://127.0.0.1:8770', ws: true },
    },
  },
  build: { outDir: 'dist', emptyOutDir: true, chunkSizeWarningLimit: 900 },
})
