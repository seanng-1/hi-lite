import { defineConfig } from 'vite'

export default defineConfig({
  server: {
    host: '0.0.0.0',
    port: 5173,
    strictPort: true,
    // Proxy API requests to Flask backend
    proxy: {
      '/data': 'http://localhost:8000',
      '/segments': 'http://localhost:8000',
      '/final': 'http://localhost:8000',
      '/api': 'http://localhost:8000'
    }
  }
})