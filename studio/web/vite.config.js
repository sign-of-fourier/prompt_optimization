import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Served by nginx at /studio/ (alias -> web/dist). API is proxied at /studio/api/.
export default defineConfig({
  plugins: [react()],
  base: '/studio/',
  server: { proxy: { '/studio/api': { target: 'http://127.0.0.1:8100', rewrite: p => p.replace(/^\/studio\/api/, '') } } },
})
