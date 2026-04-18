import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import path from 'node:path'

export default defineConfig({
  plugins: [vue()],
  // Phase 7：静态资源基路径。nginx 把 frontend/dist 挂到 /usr/share/nginx/html/admin，
  // 设 base='/admin/' 后 index.html 引用的 /admin/assets/*.js/.css 才能被
  // location /admin/ 的 try_files 命中，避免浏览器访问 /admin/login 时 JS/CSS 404。
  base: '/admin/',
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/admin': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/api/events': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 1500,
  },
})
