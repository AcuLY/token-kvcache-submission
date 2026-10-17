import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useAppStore } from '@/stores/app'

const config = {
  version: 2,
  agent_a_profile: { agent_id: 'agent-a', display_name: 'Agent A', role_description: '', system_prompt: 'A', generation_config: {} },
  agent_b_profile: { agent_id: 'agent-b', display_name: 'Agent B', role_description: '', system_prompt: 'B', generation_config: {} },
  initial_input: 'Task',
  first_speaker: 'agent-a',
  max_agent_turns: 6,
  max_context_tokens: 4096,
  stop_on_eos: true,
  mode_policy: 'always_token',
}

describe('app store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.restoreAllMocks()
  })

  it('loads CapabilityDocument-driven extensions and ready agent facts', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
      config,
      agents: [
        { agent_id: 'agent-a', instance_id: 'a1', ready: true },
        { agent_id: 'agent-b', instance_id: 'b1', ready: true },
      ],
      sessions: [],
      active_session: null,
      environment: { version: 1, environment_id: 'local', model_provider: 'model', network_provider: 'network', transfer_provider: 'token' },
      capabilities: {
        transfer_modes: ['TOKEN/FULL'],
        runtime_features: ['tokenize'],
        analysis_features: ['overview'],
        ui_extensions: [{ id: 'mock-lab', label: 'Mock 实验室', route: '/mock', kind: 'tool' }],
      },
      latest_seq: 0,
      events: [],
    }), { headers: { 'Content-Type': 'application/json' } })))

    const store = useAppStore()
    await store.loadBootstrap()

    expect(store.registeredReady).toBe(2)
    expect(store.supports('token/full')).toBe(true)
    expect(store.uiExtensions.map((item) => item.route)).toEqual(['/mock'])
  })

  it('orders live message metadata and rejects duplicate seq values', () => {
    const store = useAppStore()
    store.applyBootstrap({
      config,
      capabilities: { transfer_modes: ['TOKEN/FULL'], runtime_features: [], analysis_features: [], ui_extensions: [] },
      agents: [],
      sessions: [{ id: 'conversation-1', status: 'running', config, messages: [], transfers: [] }],
      active_session: { id: 'conversation-1', status: 'running', config, messages: [], transfers: [] },
      environment: null,
      environments: [],
      mock: null,
      latest_seq: 4,
      events: [],
    })

    const event = {
      seq: 5,
      timestamp: '2026-09-04T10:00:00Z',
      type: 'turn.generated',
      conversation_id: 'conversation-1',
      agent_id: 'agent-a',
      turn_id: 1,
      payload: { message_text: 'hello', token_count: 5, output_token_count: 2 },
    }
    store.acceptEvent(event)
    store.acceptEvent(event)

    expect(store.latestSeq).toBe(5)
    expect(store.activeSession?.messages).toHaveLength(1)
    expect(store.activeSession?.messages?.[0]?.text).toBe('hello')
    expect(store.activeSession?.messages?.[0]?.turn_index).toBe(1)
    expect(store.activeSession?.messages?.[0]?.id).toBe('agent-a-1')
    expect(store.activeSession?.messages?.[0]?.output_token_count).toBe(2)
    store.acceptEvent({ ...event, seq: 6, type: 'transfer.planned', transfer_id: 'transfer-1', payload: { mode: 'TOKEN', scope: 'FULL', payload_bytes: 128, source_agent: 'agent-a', target_agent: 'agent-b' } })
    store.acceptEvent({ ...event, seq: 7, type: 'transfer.execute_requested', transfer_id: 'transfer-1', payload: {} })
    expect(store.activeSession?.transfers?.[0]?.mode).toBe('TOKEN')
    expect(store.activeSession?.transfers?.[0]?.payload_bytes).toBe(128)
  })
})
