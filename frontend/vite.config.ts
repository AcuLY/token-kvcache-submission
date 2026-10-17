import { fileURLToPath, URL } from 'node:url'
import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const coordinator = env.VITE_COORDINATOR_URL || 'http://127.0.0.1:8000'

  return {
    plugins: [vue()],
    resolve: {
      alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
    },
    server: {
      port: Number(env.VITE_WEBUI_PORT || 5173),
      proxy: {
        '/api': {
          target: coordinator,
          changeOrigin: true,
          ws: true,
        },
      },
    },
  }
})
