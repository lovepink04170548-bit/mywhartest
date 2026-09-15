import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import os from 'node:os'
import { fileURLToPath, URL } from 'url'

// https://vite.dev/config/
function collectLocalIpv4Hosts(): string[] {
  const hosts = new Set<string>()

  try {
    for (const interfaces of Object.values(os.networkInterfaces())) {
      for (const iface of interfaces ?? []) {
        if (iface.family === 'IPv4' && !iface.internal) {
          hosts.add(iface.address)
        }
      }
    }
  } catch {
    return []
  }

  return [...hosts]
}

const extraAllowedHosts = (process.env.__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS || '')
  .split(',')
  .map((host) => host.trim())
  .filter(Boolean)

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    host: '0.0.0.0',
    allowedHosts: [
      'host.docker.internal',
      'localhost',
      '127.0.0.1',
      ...collectLocalIpv4Hosts(),
      ...extraAllowedHosts,
    ],
    proxy: {
      '/api/': {
        target: 'http://localhost:8000', // 您的后端 API 地址
        changeOrigin: true,
      },
      '/media': {
        target: 'http://localhost:8000', // 您的后端 API 地址
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://localhost:8000', // WebSocket 地址
        ws: true,
        changeOrigin: true,
      },
    },
  },
})
