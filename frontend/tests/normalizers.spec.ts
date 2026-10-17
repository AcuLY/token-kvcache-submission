import { describe, expect, it } from 'vitest'
import { normalizeBootstrap, normalizeConfig, normalizeMock, normalizeSession } from '@/types/normalizers'

const config = {
  agent_a_profile: {
    agent_id: 'alpha',
    display_name: 'Alpha',
    role_description: 'A role',
    system_prompt: 'A system',
    generation_config: { max_new_tokens: 32, temperature: 0 },
  },
  agent_b_profile: {
    agent_id: 'beta',
    display_name: 'Beta',
    role_description: 'B role',
    system_prompt: 'B system',
    generation_config: { max_new_tokens: 32, temperature: 0 },
  },
  initial_input: 'Task',
  first_speaker: 'alpha',
  max_agent_turns: 6,
  max_context_tokens: 4096,
  stop_on_eos: true,
  mode_policy: 'always_token',
}

describe('contract normalizers', () => {
  it('preserves decision calibration and disabled windows across config round trips', () => {
    const decision = {
      objective: 'p2p_only', history_window_tokens: null,
      token_prepare_ms: 3,
      token_restore_overhead_ms: 4, kv_export_overhead_ms: 5,
      kv_import_overhead_ms: 6,
    }
    expect(normalizeConfig({ ...config, decision })?.decision).toMatchObject(decision)
    expect(normalizeConfig({ ...config, decision })?.decision?.paper.layers).toBe(32)
  })

  it('keeps observed handoff total separate from common window rebuild cost', () => {
    const session = normalizeSession({
      conversation_id: 'cost-test', config,
      transfers: [{ transfer_id: 't', turn_id: 1, timings: { p2p_ms: 10 } }],
      metrics: [
        { transfer_id: 't', turn_id: 1, phase: 'handoff_e2e', duration_ms: 30 },
        { turn_id: 2, phase: 'context_rebuild', duration_ms: 12 },
      ],
    })
    expect(session.transfers?.[0]?.timings).toMatchObject({
      handoff_e2e_ms: 30, context_rebuild_ms: 12, total_ms: 22,
    })
  })
  it('unwraps a versioned config document and preserves its optimistic version', () => {
    const result = normalizeBootstrap({
      config: { version: 7, config },
      capabilities: {},
      sessions: [],
      agents: [],
    })

    expect(result.config?.agent_a_profile.agent_id).toBe('alpha')
    expect(result.config?.version).toBe(7)
  })

  it('maps backend status, messages, ledger and exact phase metrics into the UI view', () => {
    const session = normalizeSession({
      conversation_id: 'conversation-1',
      status: 'COMPLETED',
      config,
      messages: [{ turn_id: 1, agent_id: 'alpha', text: 'done', token_count: 12 }],
      ledger: [{
        transfer_id: 'transfer-1',
        turn_id: 1,
        source_agent: 'alpha',
        target_agent: 'beta',
        mode: 'TOKEN',
        scope: 'FULL',
        payload_bytes: 48,
        token_count: 12,
        predicted_p2p_ms: 9,
        network_state: { bandwidth_mbps: 100, rtt_ms: 8, jitter_ms: 1 },
      }],
      metrics: [
        { transfer_id: 'transfer-1', turn_id: 1, phase: 'p2p_ms', duration_ms: 20 },
        { transfer_id: 'transfer-1', turn_id: 1, phase: 'sender_p2p_elapsed', duration_ms: 25 },
        { transfer_id: null, turn_id: 2, phase: 'generation', duration_ms: 5 },
      ],
    })

    expect(session.status).toBe('completed')
    expect(session.messages?.[0]?.turn_index).toBe(1)
    expect(session.transfers?.[0]).toMatchObject({
      source_agent_id: 'alpha',
      target_agent_id: 'beta',
      predicted_ms: 9,
      actual_ms: 20,
      network: { bandwidth_mbps: 100 },
      timings: { p2p_ms: 20, generation_ms: 5, total_ms: 25 },
    })
  })

  it('normalizes the coordinator links and model document without inventing metrics', () => {
    const result = normalizeMock({
      version: 3,
      links: {
        'alpha->beta': { bandwidth_mbps: 50, rtt_ms: 30, jitter_ms: 4, seed: 1 },
        'beta->alpha': { bandwidth_mbps: 40, rtt_ms: 35, jitter_ms: 6, seed: 2 },
      },
      model: { tokenize_delay_ms: 1, prefill_delay_ms: 3, generate_delay_ms: 7, response_prefix: 'mock' },
    })

    expect(result?.scenarios).toHaveLength(1)
    expect(result?.model_behavior.generate_delay_ms).toBe(7)
    expect(result?.links?.['alpha->beta']?.bandwidth_mbps).toBe(50)
  })
})
