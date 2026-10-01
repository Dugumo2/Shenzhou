import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
export default defineConfig({
  base: '/app/', plugins: [vue()],
  server: {
    host: '127.0.0.1', strictPort: true,
    proxy: {
      '/api': { target: 'http://127.0.0.1:18765', changeOrigin: false },
      '/account/': { target: 'http://127.0.0.1:18765', changeOrigin: false },
      '/register/': { target: 'http://127.0.0.1:18765', changeOrigin: false },
    },
  },
  build: { sourcemap: false, target: 'es2022' },
})
