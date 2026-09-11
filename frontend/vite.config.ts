import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': { target: process.env.CAD_API_TARGET || 'http://localhost:8000', ws: true },
      '/ws': { target: process.env.CAD_API_TARGET || 'http://localhost:8000', ws: true },
    }
  }
})
