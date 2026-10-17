import type {
  AgentProfile,
  BootstrapDocument,
  CapabilityDocument,
  EnvironmentSummary,
  JsonRecord,
  MockConfig,
  ObservationEvent,
  SessionConfig,
  SessionSummary,
  UiExtension,
} from './contracts'

const record = (value: unknown): JsonRecord =>
  value !== null && typeof value === 'object' && !Array.isArray(value) ? (value as JsonRecord) : {}

const array = <T>(value: unknown): T[] => (Array.isArray(value) ? (value as T[]) : [])

const string = (value: unknown, fallback = ''): string =>
  typeof value === 'string' ? value : fallback

const number = (value: unknown, fallback = 0): number =>
  typeof value === 'number' && Number.isFinite(value) ? value : fallback

const bool = (value: unknown, fallback = false): boolean =>
  typeof value === 'boolean' ? value : fallback

function normalizeProfile(value: unknown, fallbackId: string): AgentProfile {
  const source = record(value)
  return {
    agent_id: string(source.agent_id, fallbackId),
    display_name: string(source.display_name, fallbackId),
    role_description: string(source.role_description),
    system_prompt: string(source.system_prompt),
    prompt_version: string(source.prompt_version, '1.0.0'),
    local_context_provider: string(source.local_context_provider, 'default'),
    local_context: string(source.local_context),
    generation_config: record(source.generation_config),
  }
}

export function normalizeConfig(value: unknown): SessionConfig | null {
  const source = record(value)
  if (!Object.keys(source).length) return null
  const agentA = normalizeProfile(source.agent_a_profile, 'agent-a')
  const agentB = normalizeProfile(source.agent_b_profile, 'agent-b')
  return {
    agent_a_profile: agentA,
    agent_b_profile: agentB,
    initial_input: string(source.initial_input),
    first_speaker: string(source.first_speaker, agentA.agent_id),
    max_agent_turns: number(source.max_agent_turns, 6),
    max_context_tokens: number(source.max_context_tokens, 8192),
    stop_on_eos: bool(source.stop_on_eos, true),
    mode_policy: string(source.mode_policy, 'adaptive'),
    decision: {
      objective: record(source.decision).objective === 'p2p_only' ? 'p2p_only' : record(source.decision).objective === 'mode_e2e' ? 'mode_e2e' : 'paper',
      token_prepare_ms: number(record(source.decision).token_prepare_ms, 0.1),
      token_restore_overhead_ms: number(record(source.decision).token_restore_overhead_ms, 2),
      kv_export_overhead_ms: number(record(source.decision).kv_export_overhead_ms, 2),
      kv_import_overhead_ms: number(record(source.decision).kv_import_overhead_ms, 2),
      history_window_tokens: record(source.decision).history_window_tokens === null ? null : number(record(source.decision).history_window_tokens, 1024),
      paper: {
        layers: number(record(record(source.decision).paper).layers, 32),
        heads: number(record(record(source.decision).paper).heads, 32),
        head_dim: number(record(record(source.decision).paper).head_dim, 128),
        model_dim: number(record(record(source.decision).paper).model_dim, 4096),
        ff_dim: number(record(record(source.decision).paper).ff_dim, 11008),
        vocab_size: number(record(record(source.decision).paper).vocab_size, 32000),
        receiver_flops: number(record(record(source.decision).paper).receiver_flops, 1e12),
        token_bits: number(record(record(source.decision).paper).token_bits, 32),
        kv_precision_bits: number(record(record(source.decision).paper).kv_precision_bits, 16),
        compression_ratio: number(record(record(source.decision).paper).compression_ratio, 1),
      },
    },
    version: typeof source.version === 'number' ? source.version : undefined,
  }
}

export function normalizeSession(value: unknown): SessionSummary {
  const source = record(value)
  const id = string(source.id || source.conversation_id || source.session_id)
  const metrics = array<Record<string, unknown>>(source.metrics)
  const transfers = array<Record<string, unknown>>(
    source.transfers || source.handoffs || source.ledger,
  ).map((item) => {
    const transferId = string(item.transfer_id)
    const transferMetrics = metrics.filter(
      (metric) =>
        metric.transfer_id === transferId ||
        (metric.transfer_id == null && metric.turn_id === Number(item.turn_id) + 1 && string(metric.phase).match(/local.*prefill|generat|context_rebuild/)),
    )
    const timings = Object.fromEntries(
      Object.entries(record(item.timings)).filter((entry): entry is [string, number] => typeof entry[1] === 'number'),
    )
    const network = Object.fromEntries(
      Object.entries(record(item.network)).filter(
        (entry): entry is [string, number | string] =>
          typeof entry[1] === 'number' || typeof entry[1] === 'string',
      ),
    )
    for (const metric of transferMetrics) {
      const phase = string(metric.phase).toLowerCase()
      const duration = number(metric.duration_ms, -1)
      if (duration >= 0) {
        if (phase === 'sender_prepare' || phase === 'sender_prepare_ms' || phase === 'prepare_ms') timings.sender_prepare_ms = duration
        else if (phase === 'handoff_e2e') timings.handoff_e2e_ms = duration
        else if (phase === 'context_rebuild') timings.context_rebuild_ms = duration
        else if (phase === 'p2p_ms') timings.p2p_ms = duration
        else if (phase === 'sender_p2p_elapsed' && timings.p2p_ms === undefined) timings.p2p_ms = duration
        else if (phase.match(/wire|transfer/) && timings.p2p_ms === undefined) timings.p2p_ms = duration
        else if (phase.match(/receiver.*prefill|receive.*prefill/)) timings.receiver_prefill_ms = duration
        else if (phase.match(/local.*prefill/)) timings.local_prefill_ms = duration
        else if (phase.match(/generat/)) timings.generation_ms = duration
        else if (phase.match(/total/)) timings.total_ms = duration
      }
      const values = record(metric.values)
      for (const key of ['bandwidth_mbps', 'rtt_ms', 'jitter_ms', 'version']) {
        const found = values[key]
        if (typeof found === 'number' || typeof found === 'string') network[key] = found
      }
    }
    if (timings.total_ms === undefined) {
      const parts = Object.entries(timings).filter(([key]) => key !== 'handoff_e2e_ms').map(([, value]) => value)
      if (parts.length) timings.total_ms = parts.reduce((sum, current) => sum + current, 0)
    }
    return {
      ...item,
      transfer_id: transferId,
      turn_id: item.turn_id as string | number | undefined,
      turn_index: typeof item.turn_id === 'number' ? item.turn_id : undefined,
      source_agent_id: string(item.source_agent_id || item.source_agent) || undefined,
      target_agent_id: string(item.target_agent_id || item.target_agent) || undefined,
      mode: string(item.mode) || undefined,
      variant: string(item.variant || item.scope) || undefined,
      selector_reason: string(item.selector_reason) || undefined,
      token_count: typeof item.token_count === 'number' ? item.token_count : undefined,
      payload_bytes: typeof item.payload_bytes === 'number' ? item.payload_bytes : undefined,
      predicted_ms: typeof (item.predicted_ms ?? item.predicted_p2p_ms) === 'number' ? Number(item.predicted_ms ?? item.predicted_p2p_ms) : undefined,
      actual_ms: typeof timings.p2p_ms === 'number' ? timings.p2p_ms : undefined,
      status: string(item.status, item.committed_at ? 'committed' : 'planned'),
      created_at: string(item.created_at || item.committed_at) || undefined,
      completed_at: string(item.completed_at || item.committed_at) || undefined,
      timings,
      network: Object.keys(network).length ? network : record(item.network_state),
    }
  })
  const rawStatus = string(source.status, 'pending').toLowerCase()
  const statusMap: Record<string, string> = {
    starting: 'pending',
    running: 'running',
    paused: 'paused',
    stopping: 'stopping',
    stopped: 'stopped',
    completed: 'completed',
    error: 'failed',
  }
  return {
    ...(source as unknown as SessionSummary),
    id,
    conversation_id: string(source.conversation_id || source.session_id || source.id, id),
    status: statusMap[rawStatus] || rawStatus,
    config: normalizeConfig(source.config) ?? undefined,
    messages: array<Record<string, unknown>>(source.messages).map((message) => ({
      ...message,
      id: string(
        message.id ||
          message.message_id ||
          `${message.agent_id || 'agent'}-${message.turn_id || message.turn_index || 0}`,
      ),
      agent_id: string(message.agent_id),
      display_name: string(message.display_name) || undefined,
      turn_index: number(message.turn_index ?? message.turn_id, 0),
      text: string(message.text || message.message_text),
      token_count: typeof message.token_count === 'number' ? message.token_count : undefined,
      status: string(message.status, 'completed'),
      created_at: string(message.created_at) || undefined,
      completed_at: string(message.completed_at) || undefined,
    })),
    transfers,
    handoffs: transfers,
    metrics: metrics as unknown as SessionSummary['metrics'],
    finish_reason: string(source.finish_reason || source.termination_reason) || undefined,
  }
}

export function normalizeCapabilities(value: unknown): CapabilityDocument {
  const source = record(value)
  const extensions = array<UiExtension>(source.ui_extensions)
    .filter((item) => item && typeof item === 'object')
    .map((item, index) => ({
      ...item,
      id: string(item.id, `extension-${index}`),
      label: string(item.label, string(item.id, `扩展 ${index + 1}`)),
      route: string(item.route, `/extensions/${string(item.id, String(index))}`),
    }))
  return {
    ...(source as unknown as CapabilityDocument),
    transfer_modes: array<string>(source.transfer_modes || source.transfer_handlers),
    mode_policies: array<string>(source.mode_policies),
    runtime_features: array<string>(source.runtime_features),
    analysis_features: array<string>(source.analysis_features),
    network_features: array<string>(source.network_features),
    unsupported_features: array<string>(source.unsupported_features),
    providers: record(source.providers) as Record<string, string>,
    features: record(source.features) as Record<string, boolean>,
    adapters: array(source.adapters),
    ui_extensions: extensions,
  }
}

export function normalizeEnvironment(value: unknown): EnvironmentSummary | null {
  const source = record(value)
  if (!Object.keys(source).length) return null
  return {
    ...(source as unknown as EnvironmentSummary),
    id: string(source.id || source.environment_id),
    environment_id: string(source.environment_id || source.id),
    name: string(source.name, string(source.id || source.environment_id, '当前环境')),
    adapters: array(source.adapters),
    capabilities: source.capabilities ? normalizeCapabilities(source.capabilities) : undefined,
  }
}

export function normalizeMock(value: unknown): MockConfig | null {
  const source = record(value)
  if (!Object.keys(source).length) return null
  const links = record(source.links) as unknown as MockConfig['links']
  const model = record(source.model || source.model_behavior) as unknown as MockConfig['model_behavior']
  const linkEntries = Object.entries(links || {})
  const first = linkEntries[0]?.[1]
  const second = linkEntries[1]?.[1]
  const canonicalScenario = first && second
    ? [{ id: 'current', name: '当前场景', a_to_b: first, b_to_a: second }]
    : []
  return {
    ...(source as unknown as MockConfig),
    scenarios: array(source.scenarios).length ? array(source.scenarios) : canonicalScenario,
    model_behavior: model,
    links,
    model,
  }
}

export function normalizeEvent(value: unknown): ObservationEvent | null {
  const source = record(value)
  const seq = number(source.seq, -1)
  if (seq < 0 || !source.type) return null
  return {
    ...(source as unknown as ObservationEvent),
    seq,
    timestamp: string(source.timestamp || source.created_at, new Date().toISOString()),
    type: string(source.type),
    payload: record(source.payload || source.data),
  }
}

export function normalizeBootstrap(value: unknown): BootstrapDocument {
  const source = record(value)
  const configDocument = record(source.config)
  const configSource = configDocument.config || source.config || record(source.current_config).config || source.current_config
  const activeSource = source.active_session
  const environments = array(source.environments)
    .map(normalizeEnvironment)
    .filter((item): item is EnvironmentSummary => item !== null)
  const events = array(source.events)
    .map(normalizeEvent)
    .filter((item): item is ObservationEvent => item !== null)
  const normalizedConfig = normalizeConfig(configSource)
  if (normalizedConfig && normalizedConfig.version === undefined) {
    const version = source.config_version ?? configDocument.version
    if (typeof version === 'number') normalizedConfig.version = version
  }
  return {
    capabilities: normalizeCapabilities(source.capabilities || source.capability_document),
    config: normalizedConfig,
    agents: array(source.agents),
    sessions: array(source.sessions).map(normalizeSession),
    active_session:
      activeSource && typeof activeSource === 'object' ? normalizeSession(activeSource) : null,
    environment: normalizeEnvironment(source.environment || source.current_environment),
    environments,
    mock: normalizeMock(source.mock || source.mock_config),
    latest_seq: number(source.latest_seq, events.at(-1)?.seq ?? 0),
    events,
  }
}
