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
    // 工作环境：WSL 里跑 Vite + Windows /mnt/d 挂载源码 → inotify 不工作。
    // 用 polling 兜底（每 1 秒扫一次），HMR 才会在 .vue 文件变化时触发。
    // 本机 Linux 或 Mac 跑可以删掉，性能影响 < 1% CPU。
    watch: {
      usePolling: true,
      interval: 1000,
    },
    proxy: {
      '/admin': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        bypass(req) {
          const accept = req.headers.accept || ''
          const method = (req.method || 'GET').toUpperCase()
          const isApi =
            accept.includes('application/json') ||
            !['GET', 'HEAD'].includes(method)
          if (!isApi) return req.url
        },
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
