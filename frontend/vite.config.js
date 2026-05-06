import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        timeout: 900000,       // 15 min — covers the 3-10 min pipeline run
        proxyTimeout: 900000,
      },
    },
  },
  build: {
    outDir: 'dist',
  },
})
