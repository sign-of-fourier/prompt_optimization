import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Served by nginx at VITE_STUDIO_BASE: '/' on impromptune.com, '/studio/' when mounted under a marketing site
// (and in `uvicorn app.main:app` dev mode). The API lives at <base>api/ either way.
const base = process.env.VITE_STUDIO_BASE || '/studio/'
process.env.VITE_STUDIO_NAME ||= 'Impromptune'   // also fills %VITE_STUDIO_NAME% in index.html
export default defineConfig({
  plugins: [react()],
  base,
  server: { proxy: { [base + 'api']: { target: 'http://127.0.0.1:8100', rewrite: p => p.slice((base + 'api').length) } } },
})
