import { expect, test, type Page } from '@playwright/test'

const config = {
  version: 2,
  agent_a_profile: {
    agent_id: 'agent-a',
    display_name: 'Agent A',
    role_description: '方案提出者',
    system_prompt: '提出可执行方案。',
    prompt_version: 'v1',
    local_context_provider: 'static',
    local_context: 'A context',
    generation_config: { max_new_tokens: 64, temperature: 0 },
  },
  agent_b_profile: {
    agent_id: 'agent-b',
    display_name: 'Agent B',
    role_description: '方案审阅者',
    system_prompt: '审阅并改进方案。',
    prompt_version: 'v1',
    local_context_provider: 'static',
    local_context: 'B context',
    generation_config: { max_new_tokens: 64, temperature: 0 },
  },
  initial_input: '验证 Token-only 本地闭环',
  first_speaker: 'agent-a',
  max_agent_turns: 6,
  max_context_tokens: 4096,
  stop_on_eos: true,
  mode_policy: 'always_token',
}

const session = {
  conversation_id: 'conversation-e2e',
  config_version: 2,
  config,
  status: 'RUNNING',
  turn_count: 2,
  current_speaker: 'agent-a',
  messages: [
    { turn_id: 1, agent_id: 'agent-a', text: '这是来自 Agent A 的真实接口消息。', state_id: 's1', token_count: 32, created_at: '2026-09-04T08:00:00Z' },
    { turn_id: 2, agent_id: 'agent-b', text: '这是来自 Agent B 的真实接口消息。', state_id: 's2', token_count: 58, created_at: '2026-09-04T08:00:01Z' },
  ],
  transfers: [{
    transfer_id: 'transfer-1',
    turn_id: 1,
    source_agent_id: 'agent-a',
    target_agent_id: 'agent-b',
    mode: 'TOKEN',
    variant: 'FULL',
    selector_reason: 'only_supported_mode',
    token_count: 32,
    payload_bytes: 128,
    predicted_ms: 17,
    actual_ms: 19,
    status: 'COMMITTED',
    network: { bandwidth_mbps: 100, rtt_ms: 10, jitter_ms: 1, version: 1 },
    timings: { sender_prepare_ms: 1, p2p_ms: 12, receiver_prefill_ms: 3, generation_ms: 3, total_ms: 19 },
  }],
  metrics: [],
  created_at: '2026-09-04T08:00:00Z',
  updated_at: '2026-09-04T08:00:01Z',
}

const bootstrap = {
  config,
  agents: [
    { agent_id: 'agent-a', instance_id: 'a-1', control_url: 'http://agent-a:8001', data_bind: '0.0.0.0:9101', data_advertise: 'agent-a:9101', status: 'READY', online: true, ready: true, last_heartbeat_at: '2026-09-04T08:00:01Z', runtime_descriptor: { model_id: 'mock-byte-v1', protocol_version: '1', capabilities: ['tokenize', 'prefill', 'generate', 'seal'] }, transport_capabilities: ['TOKEN/FULL'] },
    { agent_id: 'agent-b', instance_id: 'b-1', control_url: 'http://agent-b:8001', data_bind: '0.0.0.0:9102', data_advertise: 'agent-b:9102', status: 'READY', online: true, ready: true, last_heartbeat_at: '2026-09-04T08:00:01Z', runtime_descriptor: { model_id: 'mock-byte-v1', protocol_version: '1', capabilities: ['tokenize', 'prefill', 'generate', 'seal'] }, transport_capabilities: ['TOKEN/FULL'] },
  ],
  sessions: [session],
  active_session: session,
  environment: { version: 1, environment_id: 'local-mock', model_provider: 'mock_http', network_provider: 'directional_mock', transfer_provider: 'token_snapshot' },
  capabilities: {
    protocol_version: '1',
    transfer_modes: ['TOKEN/FULL'],
    runtime_features: ['tokenize', 'prefill', 'generate', 'seal'],
    analysis_features: ['overview', 'network', 'transfers', 'events', 'csv'],
    unsupported_features: ['kv', 'kv_delta', 'adaptive', 'cache_metrics'],
    providers: { model: 'mock_http', network: 'directional_mock', transfer: 'token_snapshot' },
    ui_extensions: [{ id: 'mock-lab', label: 'Mock 实验室', route: '/mock', kind: 'tool' }],
  },
  mock: {
    version: 1,
    links: {
      'agent-a->agent-b': { bandwidth_mbps: 100, rtt_ms: 10, jitter_ms: 1, seed: 7 },
      'agent-b->agent-a': { bandwidth_mbps: 80, rtt_ms: 14, jitter_ms: 2, seed: 11 },
    },
    model: { tokenize_delay_ms: 1, prefill_delay_ms: 12, generate_delay_ms: 25, response_prefix: '[mock]' },
  },
  latest_seq: 2,
  events: [
    { seq: 1, timestamp: '2026-09-04T08:00:00Z', type: 'session.started', conversation_id: 'conversation-e2e', payload: {} },
    { seq: 2, timestamp: '2026-09-04T08:00:01Z', type: 'transfer.committed', conversation_id: 'conversation-e2e', transfer_id: 'transfer-1', payload: { status: 'COMMITTED' } },
  ],
}

async function mockCoordinator(page: Page): Promise<void> {
  await page.routeWebSocket('**/api/v1/observations**', (socket) => {
    socket.send(JSON.stringify({ events: [], latest_seq: bootstrap.latest_seq }))
  })
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname === '/api/v1/bootstrap') {
      await route.fulfill({ json: bootstrap })
      return
    }
    if (url.pathname === '/api/v1/config' && route.request().method() === 'PUT') {
      const payload = route.request().postDataJSON()
      await route.fulfill({ json: { version: 3, config: payload.config || payload } })
      return
    }
    if (url.pathname === '/api/v1/mock' && route.request().method() === 'PUT') {
      await route.fulfill({ json: route.request().postDataJSON() })
      return
    }
    if (url.pathname.startsWith('/api/v1/sessions/')) {
      await route.fulfill({ json: session })
      return
    }
    if (url.pathname === '/api/v1/experiments.csv') {
      await route.fulfill({ contentType: 'text/csv', body: 'conversation_id,turn_id\nconversation-e2e,1\n' })
      return
    }
    await route.fulfill({ status: 404, json: { detail: 'not mocked' } })
  })
}

test.beforeEach(async ({ page }) => {
  await mockCoordinator(page)
})

async function navigateFromShell(page: Page, label: string): Promise<void> {
  const desktopLink = page.locator('.desktop-nav .nav-link').filter({ hasText: label })
  if (await desktopLink.isVisible()) {
    await desktopLink.click()
    return
  }
  await page.getByRole('button', { name: '打开导航' }).click()
  await page.locator('.drawer-link').filter({ hasText: label }).click()
}

test('renders the live Token conversation and capability-driven pages', async ({ page }) => {
  const pageErrors: string[] = []
  page.on('pageerror', (error) => pageErrors.push(error.message))
  await page.goto('/sessions')
  await expect(page.getByRole('heading', { name: '对话' })).toBeVisible()
  await expect(page.getByText('这是来自 Agent A 的真实接口消息。')).toBeVisible()
  await expect(page.getByText('TOKEN / FULL').first()).toBeVisible()

  await navigateFromShell(page, '实验分析')
  await page.getByText('Cache', { exact: true }).first().click()
  await expect(page.getByText('当前环境未提供 Cache / KV 能力')).toBeVisible()

  await navigateFromShell(page, 'Mock 实验室')
  await page.waitForTimeout(300)
  expect(pageErrors).toEqual([])
  await expect(page.getByRole('heading', { name: 'Mock 实验室' })).toBeVisible()
  await expect(page.getByText('A → B（从 A 到 B）')).toBeVisible()
})

test('keeps the primary workflow usable at mobile width', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/sessions')
  await expect(page.getByRole('button', { name: '打开导航' })).toBeVisible()
  await expect(page.getByRole('heading', { name: '对话' })).toBeVisible()
  const overflow = await page.evaluate(() => ({
    delta: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    offenders: [...document.querySelectorAll<HTMLElement>('body *')]
      .map((element) => ({
        tag: element.tagName,
        className: element.className,
        left: Math.round(element.getBoundingClientRect().left),
        right: Math.round(element.getBoundingClientRect().right),
        width: Math.round(element.getBoundingClientRect().width),
        scrollWidth: element.scrollWidth,
        clientWidth: element.clientWidth,
      }))
      .filter((item) => item.right > window.innerWidth + 1 || item.left < -1 || item.width > window.innerWidth + 1 || item.scrollWidth > item.clientWidth + 1)
      .sort((a, b) => Math.max(b.width - window.innerWidth, -b.left, b.right - window.innerWidth) - Math.max(a.width - window.innerWidth, -a.left, a.right - window.innerWidth))
      .slice(0, 20),
  }))
  expect(overflow.delta, JSON.stringify(overflow.offenders, null, 2)).toBeLessThanOrEqual(1)
})
