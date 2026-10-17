export type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue | undefined }
export type JsonRecord = Record<string, unknown>

export type AgentId = string
export type SessionStatus = 'pending' | 'running' | 'paused' | 'completed' | 'stopped' | 'failed' | string

export interface RuntimeDescriptor {
  provider?: string
  runtime_id?: string
  model_id?: string
  revision?: string
  tokenizer_id?: string
  tokenizer_revision?: string
  tokenizer_type?: string
  protocol_version?: string
  capabilities?: string[]
  [key: string]: unknown
}

export interface AgentRegistration {
  agent_id: AgentId
  instance_id?: string
  display_name?: string
  status?: string
  ready?: boolean
  online?: boolean
  last_heartbeat_at?: string
  heartbeat_interval_seconds?: number
  control_url?: string
  control_endpoint?: string
  data_bind?: string
  data_advertise?: string
  p2p_bind_endpoint?: string
  p2p_advertise_endpoint?: string
  runtime?: RuntimeDescriptor
  runtime_descriptor?: RuntimeDescriptor
  transfer_capabilities?: string[]
  transport_capabilities?: string[]
  runtime_capabilities?: string[]
  [key: string]: unknown
}

export interface GenerationConfig {
  temperature?: number
  top_p?: number
  max_output_tokens?: number
  repetition_penalty?: number
  [key: string]: unknown
}

export interface AgentProfile {
  agent_id: AgentId
  display_name: string
  role_description: string
  system_prompt: string
  prompt_version?: string
  local_context_provider?: string
  local_context?: string
  generation_config: GenerationConfig
}

export interface PaperModelConfig {
  layers: number
  heads: number
  head_dim: number
  model_dim: number
  ff_dim: number
  vocab_size: number
  receiver_flops: number
  token_bits: number
  kv_precision_bits: number
  compression_ratio: number
}

export interface DecisionSettings {
  objective: 'paper' | 'mode_e2e' | 'p2p_only'
  paper: PaperModelConfig
  token_prepare_ms: number
  token_restore_overhead_ms: number
  kv_export_overhead_ms: number
  kv_import_overhead_ms: number
  history_window_tokens: number | null
}

export interface CandidateCost {
  paper_bits: number
  paper_ms: number
  paper_applicable: boolean
  prefill_tokens: number
  mode: string
  scope: string
  payload_bytes: number
  frame_bytes: number
  sender_ms: number
  p2p_ms: number
  receiver_ms: number
  mode_e2e_ms: number
  objective_ms: number
}

export interface DecisionRecord {
  effective_objective: string
  theta_tokens: number
  wrapper_tokens: number
  objective: string
  candidates: CandidateCost[]
  token_minus_kv_ms: number
  reason: string
  cache_prefix_tokens: number
  xi_tokens: number
  current_tokens: number
}

export interface SessionConfig {
  decision?: DecisionSettings
  agent_a_profile: AgentProfile
  agent_b_profile: AgentProfile
  initial_input: string
  first_speaker: AgentId
  max_agent_turns: number
  max_context_tokens: number
  stop_on_eos: boolean
  mode_policy: string
  version?: number
}

export interface ConversationMessage {
  output_token_count?: number
  id?: string
  agent_id: AgentId
  display_name?: string
  turn_index: number
  text: string
  token_count?: number
  status?: string
  created_at?: string
  completed_at?: string
}

export interface PhaseTimings {
  sender_prepare_ms?: number
  prepare_ms?: number
  p2p_ms?: number
  receiver_prefill_ms?: number
  local_prefill_ms?: number
  generation_ms?: number
  total_ms?: number
  [key: string]: number | undefined
}

export interface NetworkState {
  direction?: string
  bandwidth_mbps?: number
  rtt_ms?: number
  jitter_ms?: number
  sampled_jitter_ms?: number
  sampled_latency_ms?: number
  version?: number | string
  captured_at?: string
  [key: string]: unknown
}

export interface TransferRecord {
  output_token_count?: number
  wrapper_token_count?: number
  receiver_known_tokens?: number
  receiver_cache_tokens?: number
  recovery_reason?: string | null
  decision?: DecisionRecord | null
  context_epoch?: number
  trimmed_tokens?: number
  start_token?: number
  xi_tokens?: number
  base_state_id?: string | null
  transfer_id: string
  turn_id?: string | number
  turn_index?: number
  source_agent_id?: AgentId
  target_agent_id?: AgentId
  direction?: string
  mode?: string
  variant?: string
  selector_reason?: string
  state_id?: string
  parent_state_id?: string
  token_count?: number
  payload_bytes?: number
  predicted_ms?: number
  actual_ms?: number
  status?: string
  created_at?: string
  completed_at?: string
  network?: NetworkState
  timings?: PhaseTimings
}

export interface SessionSummary {
  protocol_version?: string
  initial_state?: { known_token_count: number; known_token_digest: string }
  id: string
  conversation_id?: string
  title?: string
  status: SessionStatus
  created_at?: string
  started_at?: string
  updated_at?: string
  completed_at?: string
  termination_reason?: string
  current_speaker?: AgentId
  turn_count?: number
  current_turn?: number
  config_version?: number
  config?: SessionConfig
  messages?: ConversationMessage[]
  transfers?: TransferRecord[]
  handoffs?: TransferRecord[]
  metrics?: MetricRecord[]
  pending_transfer_id?: string
  finish_reason?: string
}

export interface MetricRecord {
  phase: string
  duration_ms: number
  agent_id?: string
  turn_id?: number
  transfer_id?: string
  recorded_at?: string
  values?: Record<string, string | number | boolean | null>
}

export interface UiExtension {
  id: string
  label: string
  route: string
  kind?: string
  icon?: string
  order?: number
  [key: string]: unknown
}

export interface AdapterSummary {
  id?: string
  kind: string
  name: string
  provider?: string
  description?: string
  endpoint?: string
  status?: string
  enabled?: boolean
  capabilities?: string[]
  dependencies?: string[]
  configurable?: boolean
  ui_extension_id?: string
  [key: string]: unknown
}

export interface CapabilityDocument {
  protocol_version?: string
  transfer_modes: string[]
  mode_policies?: string[]
  runtime_features: string[]
  analysis_features: string[]
  network_features?: string[]
  unsupported_features?: string[]
  providers?: Record<string, string>
  features?: Record<string, boolean>
  adapters?: AdapterSummary[]
  ui_extensions: UiExtension[]
}

export interface EnvironmentSummary {
  id: string
  name: string
  environment_id?: string
  model_provider?: string
  network_provider?: string
  transfer_provider?: string
  version?: number
  enabled?: boolean
  current?: boolean
  status?: string
  description?: string
  updated_at?: string
  adapters?: AdapterSummary[]
  capabilities?: CapabilityDocument
  [key: string]: unknown
}

export interface MockDirectionConfig {
  enabled?: boolean
  bandwidth_mbps: number
  rtt_ms: number
  jitter_ms: number
  seed?: number
}

export interface MockScenario {
  id: string
  name: string
  description?: string
  severity?: string
  a_to_b: MockDirectionConfig
  b_to_a: MockDirectionConfig
  updated_at?: string
}

export interface MockModelBehavior {
  prefill_per_token_ms?: number
  cache_export_delay_ms?: number
  cache_import_delay_ms?: number
  tokenize_delay_ms: number
  prefill_delay_ms: number
  generate_delay_ms: number
  response_seed?: number
  response_prefix?: string
  [key: string]: unknown
}

export interface MockConfig {
  version?: number | string
  seed?: number
  active_scenario_id?: string
  scenarios: MockScenario[]
  model_behavior: MockModelBehavior
  links?: Record<string, MockDirectionConfig>
  model?: MockModelBehavior
  updated_at?: string
}

export interface ObservationEvent {
  seq: number
  timestamp: string
  type: string
  conversation_id?: string
  session_id?: string
  turn_id?: string | number
  transfer_id?: string
  agent_id?: AgentId
  payload?: JsonRecord
}

export interface BootstrapDocument {
  capabilities: CapabilityDocument
  config: SessionConfig | null
  agents: AgentRegistration[]
  sessions: SessionSummary[]
  active_session: SessionSummary | null
  environment: EnvironmentSummary | null
  environments: EnvironmentSummary[]
  mock: MockConfig | null
  latest_seq: number
  events?: ObservationEvent[]
}

export interface ApiErrorBody {
  detail?: string | { msg?: string; message?: string }[]
  message?: string
  code?: string
}
