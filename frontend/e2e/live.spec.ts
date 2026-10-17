import { expect, test } from '@playwright/test'
import { mkdir } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

test('adaptive selection exposes costs and bounded cache debt', async ({ page, request }) => {
  const savedConfig = await (await request.get('/api/v1/config')).json()
  const savedMock = await (await request.get('/api/v1/mock')).json()
  const errors: string[] = []
  const task = `Adaptive ${Date.now()}`
  page.on('pageerror', error => errors.push(error.message))
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()) })
  try {
    const mock = structuredClone(savedMock)
    mock.model.prefill_delay_ms = 100
    mock.model.generate_delay_ms = 0
    for (const [direction, value] of Object.entries(mock.links)) {
      Object.assign(value as object, { bandwidth_mbps: direction.startsWith('agent-a') ? 0.01 : 1000, rtt_ms: 1, jitter_ms: 0 })
    }
    expect((await request.put('/api/v1/mock', { data: mock })).ok()).toBeTruthy()
    expect((await request.put('/api/v1/config', { data: { ...savedConfig.config, mode_policy: 'always_token', version: savedConfig.version } })).ok()).toBeTruthy()
    await page.goto('/agents')
    await page.locator('label').filter({ has: page.locator('span', { hasText: /^传输策略$/ }) }).locator('.n-select').click()
    await page.getByText('自适应 Token / KV', { exact: true }).last().click()
    await page.getByPlaceholder('输入任务；也可在开始会话时由调用方覆盖。').fill(task)
    await page.locator('label').filter({ hasText: '共享历史窗口（Tokens）' }).getByRole('textbox').fill('700')
    await page.getByRole('button', { name: '保存配置', exact: true }).click()
    await expect(page.getByRole('button', { name: '保存配置', exact: true })).toBeDisabled()
    const config = await (await request.get('/api/v1/config')).json()
    expect(config.config.decision.history_window_tokens).toBe(700)
    await page.goto('/sessions')
    await page.getByRole('button', { name: '新会话并开始', exact: true }).first().click()
    await expect(page.locator('.session-title-line h1')).toHaveText(task)
    await expect(page.locator('.conversation-panel article')).toHaveCount(6, { timeout: 30000 })
    await expect(page.getByRole('button', { name: '暂停', exact: true })).toBeDisabled()
    await page.goto('/experiments')
    await page.locator('.filter-bar .n-select').first().click()
    await page.getByText(task, { exact: true }).last().click()
    await page.getByText('自适应决策', { exact: true }).first().click()
    await expect(page.getByRole('heading', { name: 'Token / KV 候选成本' })).toBeVisible()
    await expect(page.getByRole('cell', { name: /^已选择/ })).toHaveCount(5)
    const bootstrap = await (await request.get('/api/v1/bootstrap')).json()
    const session = bootstrap.sessions.find((row: { config: { initial_input: string } }) => row.config.initial_input === task)
    expect(new Set(session.transfers.map((row: { mode: string }) => row.mode))).toEqual(new Set(['TOKEN', 'KV']))
    expect(session.transfers.some((row: { trimmed_tokens: number }) => row.trimmed_tokens > 0)).toBeTruthy()
    const snapshots = join(tmpdir(), 'token-kvcache-qa')
    await mkdir(snapshots, { recursive: true })
    for (const width of [1536, 390]) {
      await page.setViewportSize({ width, height: 900 })
      await page.waitForTimeout(400)
      expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1)
      await page.screenshot({ path: join(snapshots, `adaptive-${width}.png`), animations: 'disabled', fullPage: true })
    }
    expect(errors).toEqual([])
  } finally {
    const latestConfig = await (await request.get('/api/v1/config')).json()
    expect((await request.put('/api/v1/config', { data: { ...savedConfig.config, version: latestConfig.version } })).ok()).toBeTruthy()
    const latestMock = await (await request.get('/api/v1/mock')).json()
    expect((await request.put('/api/v1/mock', { data: { ...savedMock, version: latestMock.version } })).ok()).toBeTruthy()
  }
})

test('KV configuration runs full then delta and renders cache observations', async ({ page, request }) => {
  const taskText = `KV 通信自动化验收 ${Date.now()}`
  const saved = await (await request.get('/api/v1/config')).json()
  expect((await request.put('/api/v1/config', { data: { ...saved.config, decision: { ...saved.config.decision, history_window_tokens: null }, mode_policy: 'always_token', version: saved.version } })).ok()).toBeTruthy()
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()) })
  const snapshots = join(tmpdir(), 'token-kvcache-qa')
  await mkdir(snapshots, { recursive: true })
  try {
    await page.goto('/agents')
    await expect(page.getByRole('heading', { name: 'Agent 与配置', exact: true })).toBeVisible()
    await page.locator('label').filter({ has: page.locator('span', { hasText: /^传输策略$/ }) }).locator('.n-select').click()
    await page.getByText('始终 KV（优先增量）', { exact: true }).last().click()
    await page.getByPlaceholder('输入任务；也可在开始会话时由调用方覆盖。').fill(taskText)
    await page.getByRole('button', { name: '保存配置', exact: true }).click()
    await expect(page.getByRole('button', { name: '保存配置', exact: true })).toBeDisabled()
    await page.goto('/sessions')
    await page.getByRole('button', { name: '新会话并开始', exact: true }).first().click()
    await expect(page.locator('.session-title-line h1')).toHaveText(taskText)
    await expect(page.locator('.conversation-panel article')).toHaveCount(6, { timeout: 30000 })
    await expect(page.getByRole('button', { name: '暂停', exact: true })).toBeDisabled()
    await page.goto('/experiments')
    await page.locator('.filter-bar .n-select').first().click()
    await page.getByText(taskText, { exact: true }).last().click()
    await page.getByText('Cache', { exact: true }).first().click()
    await expect(page.getByRole('heading', { name: 'Mock KV 传输' })).toBeVisible()
    await expect(page.getByRole('cell', { name: 'KV / DELTA', exact: true })).toHaveCount(5)
    await expect(page.getByRole('cell', { name: 'KV / FULL', exact: true })).toHaveCount(0)
    for (const width of [1536, 390]) {
      await page.setViewportSize({ width, height: 900 })
      expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1)
      await page.waitForTimeout(400)
      await page.screenshot({ animations: 'disabled', path: join(snapshots, `kv-cache-${width}.png`), fullPage: true })
    }
    expect(errors).toEqual([])
  } finally {
    const latest = await (await request.get('/api/v1/config')).json()
    expect((await request.put('/api/v1/config', { data: { ...saved.config, version: latest.version } })).ok()).toBeTruthy()
  }
})

test('real services: configuration, conversation, replay, analysis and responsive pages', async ({ page, request }) => {
  const errors: string[] = []
  const taskText = `Token 通信自动化验收 ${Date.now()}`
  page.on('pageerror', error => errors.push(error.message))
  const tokenConfig = await (await request.get('/api/v1/config')).json()
  expect((await request.put('/api/v1/config', { data: { ...tokenConfig.config, mode_policy: 'always_token', version: tokenConfig.version } })).ok()).toBeTruthy()
  const savedMock = await (await request.get('/api/v1/mock')).json()
  const snapshotDir = join(tmpdir(), 'token-kvcache-qa')
  await mkdir(snapshotDir, { recursive: true })
  try {
    const mock = structuredClone(savedMock)
    mock.model.generate_delay_ms = 350
    mock.model.prefill_delay_ms = 30
    expect((await request.put('/api/v1/mock', { data: mock })).ok()).toBeTruthy()
    await page.goto('/agents')
    await expect(page.getByRole('heading', { name: 'Agent 与配置', exact: true })).toBeVisible()
    await page.getByPlaceholder('输入任务；也可在开始会话时由调用方覆盖。').fill(taskText)
    await page.getByRole('button', { name: '保存配置', exact: true }).click()
    await expect(page.getByRole('button', { name: '保存配置', exact: true })).toBeDisabled()
    await page.goto('/sessions')
    await page.getByRole('button', { name: '新会话并开始', exact: true }).first().click()
    await expect(page.getByRole('button', { name: '暂停', exact: true })).toBeEnabled()
    await page.getByRole('button', { name: '暂停', exact: true }).click()
    await expect(page.getByRole('button', { name: '继续', exact: true })).toBeEnabled()
    await page.waitForTimeout(900)
    await page.getByRole('button', { name: '继续', exact: true }).click()
    await expect(page.locator('.conversation-panel article')).toHaveCount(6, { timeout: 20000 })
    await expect(page.getByRole('button', { name: '暂停', exact: true })).toBeDisabled({ timeout: 10000 })
    const bootstrap = await (await request.get('/api/v1/bootstrap')).json()
    const current = bootstrap.sessions.find((session: {config: {initial_input: string}}) => session.config.initial_input === taskText)
    expect(current.status).toBe('COMPLETED')
    expect(current.ledger).toHaveLength(5)
    expect(current.transfers.every((transfer: {mode: string}) => transfer.mode === 'TOKEN')).toBeTruthy()
    await page.reload()
    await expect(page.getByRole('heading', { name: '对话', exact: true })).toBeVisible()
    for (const [name, route] of [['conversation', '/sessions'], ['agents', '/agents'], ['experiments', '/experiments'], ['environment', '/environment'], ['mock', '/mock']]) {
      await page.goto(route!)
      await page.waitForTimeout(300)
      await expect(page.locator('h1').first()).toBeVisible()
      await page.screenshot({ path: join(snapshotDir, `${name}-desktop.png`), fullPage: true })
      for (const width of [1024, 390]) {
        await page.setViewportSize({ width, height: 844 })
        await page.waitForTimeout(100)
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
        expect(overflow, `${route} at ${width}px`).toBeLessThanOrEqual(1)
        await page.screenshot({ path: join(snapshotDir, `${name}-${width}.png`), fullPage: true })
      }
      await page.setViewportSize({ width: 1536, height: 1024 })
    }
    await page.goto('/experiments')
    await page.locator('.filter-bar .n-select').first().click()
    await page.getByText(taskText, { exact: true }).last().click()
    await page.getByText('Cache', { exact: true }).first().click()
    await expect(page.getByText('暂无 Cache 观测数据')).toBeVisible()
    await page.getByText('概览', { exact: true }).first().click()
    const download = page.waitForEvent('download')
    await page.getByRole('button', { name: '导出 CSV', exact: true }).click()
    expect((await download).suggestedFilename()).toContain('.csv')
    expect(errors).toEqual([])
    console.log(`Screenshots: ${snapshotDir}`)
  } finally {
    const latestMock = await (await request.get('/api/v1/mock')).json()
    await request.put('/api/v1/mock', { data: { ...savedMock, version: latestMock.version } })
  }
})
