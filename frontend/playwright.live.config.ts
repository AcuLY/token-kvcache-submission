import { defineConfig } from '@playwright/test'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

export default defineConfig({
  testDir: './e2e',
  testMatch: 'live.spec.ts',
  workers: 1,
  timeout: 60000,
  reporter: 'list',
  outputDir: join(tmpdir(), 'token-kvcache-live-results'),
  use: {
    baseURL: process.env.E2E_BASE_URL || 'http://127.0.0.1:18080',
    headless: true,
    viewport: { width: 1536, height: 1024 },
  },
})
