import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { api, downloadBlob, unwrapResource, type SessionCommand } from '@/api/http'
import { ObservationSocket, type RealtimeStatus } from '@/realtime/ws'
import { mergeConfigPrompts } from '@/utils/prompts'
import type {
  AgentRegistration,
  BootstrapDocument,
  CapabilityDocument,
  ConversationMessage,
  EnvironmentSummary,
  MockConfig,
  ObservationEvent,
  SessionConfig,
  SessionSummary,
  TransferRecord,
} from '@/types/contracts'
import {
  normalizeBootstrap,
  normalizeConfig,
  normalizeEnvironment,
  normalizeEvent,
  normalizeMock,
  normalizeSession,
} from '@/types/normalizers'

const EMPTY_CAPABILITIES: CapabilityDocument = {
  transfer_modes: [],
  runtime_features: [],
  analysis_features: [],
  ui_extensions: [],
}

const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T

const eventSessionId = (event: ObservationEvent): string | undefined =>
  event.conversation_id || event.session_id || (event.payload?.conversation_id as string | undefined)

const isAgentReady = (agent: AgentRegistration): boolean => {
  if (typeof agent.ready === 'boolean') return agent.ready
  if (typeof agent.online === 'boolean' && !agent.online) return false
  return ['ready', 'online', 'healthy'].includes(String(agent.status || '').toLowerCase())
}

function messageFromEvent(event: ObservationEvent): ConversationMessage | null {
  const payload = event.payload || {}
  const nested = payload.message
  if (nested && typeof nested === 'object' && !Array.isArray(nested)) {
    return nested as unknown as ConversationMessage
  }
  const text = typeof payload.text === 'string'
    ? payload.text
    : typeof payload.message_text === 'string'
      ? payload.message_text
      : undefined
  if (!text || !event.type.toLowerCase().match(/message|generat/)) return null
  return {
    id: typeof payload.message_id === 'string' ? payload.message_id : `${event.agent_id || payload.agent_id || ''}-${event.turn_id ?? payload.turn_index ?? payload.turn ?? 0}`,
    agent_id: event.agent_id || String(payload.agent_id || ''),
    display_name: typeof payload.display_name === 'string' ? payload.display_name : undefined,
    turn_index: Number(event.turn_id ?? payload.turn_index ?? payload.turn ?? 0),
    text,
    token_count: typeof payload.token_count === 'number' ? payload.token_count : undefined,
    output_token_count: typeof payload.output_token_count === 'number' ? payload.output_token_count : undefined,
    status: typeof payload.status === 'string' ? payload.status : 'completed',
    created_at: event.timestamp,
  }
}

function transferFromEvent(event: ObservationEvent): TransferRecord | null {
  const payload = event.payload || {}
  const nested = payload.transfer || payload.handoff
  if (nested && typeof nested === 'object' && !Array.isArray(nested)) {
    return nested as unknown as TransferRecord
  }
  const id = event.transfer_id || (typeof payload.transfer_id === 'string' ? payload.transfer_id : undefined)
  if (!id || !event.type.toLowerCase().match(/transfer|handoff|commit/)) return null
  return {
    transfer_id: id,
    turn_id: event.turn_id,
    source_agent_id:
      typeof (payload.source_agent_id || payload.source_agent) === 'string'
        ? String(payload.source_agent_id || payload.source_agent)
        : undefined,
    target_agent_id:
      typeof (payload.target_agent_id || payload.target_agent) === 'string'
        ? String(payload.target_agent_id || payload.target_agent)
        : undefined,
    direction: typeof payload.direction === 'string' ? payload.direction : undefined,
    mode: typeof payload.mode === 'string' ? payload.mode : undefined,
    variant:
      typeof (payload.variant || payload.scope) === 'string'
        ? String(payload.variant || payload.scope)
        : undefined,
    selector_reason: typeof payload.selector_reason === 'string' ? payload.selector_reason : undefined,
    token_count: typeof payload.token_count === 'number' ? payload.token_count : undefined,
    payload_bytes: typeof payload.payload_bytes === 'number' ? payload.payload_bytes : undefined,
    predicted_ms: typeof payload.predicted_p2p_ms === 'number' ? payload.predicted_p2p_ms : undefined,
    status: typeof payload.status === 'string' ? payload.status : event.type,
    created_at: event.timestamp,
    network: payload.network as unknown as TransferRecord['network'],
    timings: payload.timings as unknown as TransferRecord['timings'],
  }
}

export const useAppStore = defineStore('app', () => {
  const loading = ref(false)
  const initialized = ref(false)
  const error = ref<string | null>(null)
  const actionError = ref<string | null>(null)
  const actionPending = ref<string | null>(null)
  const capabilities = ref<CapabilityDocument>(clone(EMPTY_CAPABILITIES))
  const config = ref<SessionConfig | null>(null)
  const savedConfig = ref<SessionConfig | null>(null)
  const agents = ref<AgentRegistration[]>([])
  const sessions = ref<SessionSummary[]>([])
  const activeSession = ref<SessionSummary | null>(null)
  const selectedSessionId = ref<string | null>(null)
  const environment = ref<EnvironmentSummary | null>(null)
  const environments = ref<EnvironmentSummary[]>([])
  const mockConfig = ref<MockConfig | null>(null)
  const events = ref<ObservationEvent[]>([])
  const latestSeq = ref(0)
  const realtimeStatus = ref<RealtimeStatus>('idle')
  let socket: ObservationSocket | undefined
  let refreshTimer: number | undefined

  const selectedSession = computed<SessionSummary | null>(() => {
    if (selectedSessionId.value) {
      return sessions.value.find((item) => item.id === selectedSessionId.value) ||
        (activeSession.value?.id === selectedSessionId.value ? activeSession.value : null)
    }
    return activeSession.value || sessions.value[0] || null
  })

  const active = computed(() =>
    activeSession.value && ['pending', 'running', 'paused'].includes(activeSession.value.status)
      ? activeSession.value
      : null,
  )

  const allTransfers = computed<TransferRecord[]>(() => {
    const records = sessions.value.flatMap((session) => session.transfers || session.handoffs || [])
    if (activeSession.value && !sessions.value.some((item) => item.id === activeSession.value?.id)) {
      records.push(...(activeSession.value.transfers || activeSession.value.handoffs || []))
    }
    const seen = new Set<string>()
    return records.filter((item) => {
      if (!item.transfer_id || seen.has(item.transfer_id)) return false
      seen.add(item.transfer_id)
      return true
    })
  })

  const registeredReady = computed(() => agents.value.filter(isAgentReady).length)
  const configDirty = computed(() => JSON.stringify(config.value) !== JSON.stringify(savedConfig.value))
  const uiExtensions = computed(() =>
    [...capabilities.value.ui_extensions].sort((a, b) => (a.order ?? 100) - (b.order ?? 100)),
  )

  function supports(name: string): boolean {
    const needle = name.toLowerCase().replaceAll('-', '_')
    const lists = [
      capabilities.value.transfer_modes,
      capabilities.value.mode_policies || [],
      capabilities.value.runtime_features,
      capabilities.value.analysis_features,
      capabilities.value.network_features || [],
    ]
    if (capabilities.value.features?.[name] || capabilities.value.features?.[needle]) return true
    return lists.flat().some((item) => String(item).toLowerCase().replaceAll('-', '_') === needle)
  }

  function applyBootstrap(document: BootstrapDocument): void {
    capabilities.value = document.capabilities
    config.value = document.config ? clone(document.config) : null
    savedConfig.value = document.config ? clone(document.config) : null
    agents.value = document.agents
    sessions.value = document.sessions
    activeSession.value = document.active_session
    environment.value = document.environment
    environments.value = document.environments.length
      ? document.environments
      : document.environment
        ? [document.environment]
        : []
    mockConfig.value = document.mock
    events.value = document.events || []
    latestSeq.value = Math.max(document.latest_seq, events.value.at(-1)?.seq || 0)
    if (!selectedSessionId.value) {
      selectedSessionId.value = document.active_session?.id || document.sessions[0]?.id || null
    }
  }

  async function loadBootstrap(options: { preserveDraft?: boolean } = {}): Promise<void> {
    loading.value = !initialized.value
    error.value = null
    try {
      const previousDraft = options.preserveDraft && configDirty.value && config.value ? clone(config.value) : null
      const document = normalizeBootstrap(await api.bootstrap())
      applyBootstrap(document)
      if (previousDraft) config.value = previousDraft
      initialized.value = true
    } catch (caught) {
      error.value = caught instanceof Error ? caught.message : '无法加载实验台数据'
      initialized.value = true
    } finally {
      loading.value = false
    }
  }

  function selectSession(id: string): void {
    selectedSessionId.value = id
    void refreshSession(id)
  }

  async function refreshSession(id: string): Promise<void> {
    try {
      const value = unwrapResource<unknown>(await api.getSession(id), ['session'])
      const session = normalizeSession(value)
      const index = sessions.value.findIndex((item) => item.id === session.id)
      if (index >= 0) sessions.value[index] = session
      else sessions.value.unshift(session)
      if (activeSession.value?.id === session.id || ['pending', 'running', 'paused'].includes(session.status)) {
        activeSession.value = session
      }
    } catch {
      // A dropped refresh must not erase the last valid observation snapshot.
    }
  }

  async function saveConfig(): Promise<boolean> {
    if (!config.value) return false
    actionPending.value = 'save-config'
    actionError.value = null
    try {
      const response = await api.saveConfig(mergeConfigPrompts(config.value))
      const value = unwrapResource<unknown>(response, ['config'])
      const normalized = normalizeConfig(value) || clone(config.value)
      if (response && typeof response === 'object' && typeof (response as Record<string, unknown>).version === 'number') {
        normalized.version = (response as Record<string, number>).version
      }
      config.value = clone(normalized)
      savedConfig.value = clone(normalized)
      return true
    } catch (caught) {
      actionError.value = caught instanceof Error ? caught.message : '保存配置失败'
      return false
    } finally {
      actionPending.value = null
    }
  }

  function restoreConfig(): void {
    config.value = savedConfig.value ? clone(savedConfig.value) : null
    actionError.value = null
  }

  async function createSession(): Promise<SessionSummary | null> {
    if (!config.value) return null
    actionPending.value = 'create-session'
    actionError.value = null
    try {
      const value = unwrapResource<unknown>(await api.createSession(savedConfig.value || config.value), ['session'])
      const session = normalizeSession(value)
      activeSession.value = session
      selectedSessionId.value = session.id
      sessions.value = [session, ...sessions.value.filter((item) => item.id !== session.id)]
      return session
    } catch (caught) {
      actionError.value = caught instanceof Error ? caught.message : '新建会话失败'
      return null
    } finally {
      actionPending.value = null
    }
  }

  async function commandSession(command: SessionCommand): Promise<boolean> {
    const target = activeSession.value
    if (!target) return false
    actionPending.value = `session-${command}`
    actionError.value = null
    try {
      const value = unwrapResource<unknown>(await api.sessionCommand(target.id, command), ['session'])
      if (value && typeof value === 'object') {
        const session = normalizeSession(value)
        activeSession.value = session
        sessions.value = [session, ...sessions.value.filter((item) => item.id !== session.id)]
      } else {
        await refreshSession(target.id)
      }
      return true
    } catch (caught) {
      actionError.value = caught instanceof Error ? caught.message : `会话${command}失败`
      return false
    } finally {
      actionPending.value = null
    }
  }

  async function saveEnvironment(next: EnvironmentSummary): Promise<boolean> {
    actionPending.value = 'save-environment'
    actionError.value = null
    try {
      const value = unwrapResource<unknown>(await api.saveEnvironment(next), ['environment'])
      environment.value = normalizeEnvironment(value) || next
      return true
    } catch (caught) {
      actionError.value = caught instanceof Error ? caught.message : '保存环境失败'
      return false
    } finally {
      actionPending.value = null
    }
  }

  async function reloadMock(): Promise<void> {
    try {
      mockConfig.value = normalizeMock(unwrapResource(await api.getMock(), ['mock', 'config']))
    } catch (caught) {
      actionError.value = caught instanceof Error ? caught.message : '加载 Mock 配置失败'
    }
  }

  async function saveMock(next: MockConfig): Promise<boolean> {
    actionPending.value = 'save-mock'
    actionError.value = null
    try {
      const value = unwrapResource<unknown>(await api.saveMock(next), ['mock', 'config'])
      mockConfig.value = normalizeMock(value) || clone(next)
      return true
    } catch (caught) {
      actionError.value = caught instanceof Error ? caught.message : '保存 Mock 配置失败'
      return false
    } finally {
      actionPending.value = null
    }
  }

  async function exportCsv(sessionId?: string): Promise<void> {
    actionPending.value = 'export-csv'
    actionError.value = null
    try {
      const blob = await api.exportCsv(sessionId)
      downloadBlob(blob, `token-kvcache-${sessionId || 'all'}-metrics.csv`)
    } catch (caught) {
      actionError.value = caught instanceof Error ? caught.message : '导出 CSV 失败'
    } finally {
      actionPending.value = null
    }
  }

  function scheduleSessionRefresh(id?: string): void {
    if (!id) return
    if (refreshTimer) window.clearTimeout(refreshTimer)
    refreshTimer = window.setTimeout(() => void refreshSession(id), 250)
  }

  function acceptEvent(value: unknown): void {
    const event = normalizeEvent(value)
    if (!event || event.seq <= latestSeq.value) return
    if (event.seq > latestSeq.value + 1 && latestSeq.value > 0) {
      void loadBootstrap({ preserveDraft: true })
      return
    }
    events.value.push(event)
    if (events.value.length > 1000) events.value.splice(0, events.value.length - 1000)
    latestSeq.value = event.seq

    if (event.type === 'agent.heartbeat' && event.agent_id) {
      const registered = agents.value.find((agent) => agent.agent_id === event.agent_id)
      if (registered) {
        registered.last_heartbeat_at = event.timestamp
        registered.online = true
        registered.ready = event.payload?.ready === true
        registered.status = String(event.payload?.status || registered.status || '')
      }
    }

    const sessionId = eventSessionId(event)
    const session = sessionId
      ? sessions.value.find((item) => item.id === sessionId) ||
        (activeSession.value?.id === sessionId ? activeSession.value : undefined)
      : activeSession.value || undefined
    if (session) {
      const next = clone(session)
      const message = messageFromEvent(event)
      if (message) {
        const messages = next.messages || []
        const messageId = message.id || `${message.agent_id}-${message.turn_index}`
        next.messages = [...messages.filter((item) => (item.id || `${item.agent_id}-${item.turn_index}`) !== messageId), message]
          .sort((a, b) => a.turn_index - b.turn_index)
      }
      const transfer = transferFromEvent(event)
      if (transfer) {
        const transfers = next.transfers || next.handoffs || []
        const existing = transfers.find((item) => item.transfer_id === transfer.transfer_id)
        const definedTransfer = Object.fromEntries(
          Object.entries(transfer).filter(([, value]) => value !== undefined),
        ) as Partial<TransferRecord>
        const merged = existing
          ? {
              ...existing,
              ...definedTransfer,
              network: { ...existing.network, ...transfer.network },
              timings: { ...existing.timings, ...transfer.timings },
            }
          : transfer
        next.transfers = [...transfers.filter((item) => item.transfer_id !== transfer.transfer_id), merged]
        next.handoffs = next.transfers
      }
      const status = event.payload?.status
      if (typeof status === 'string' && event.type.toLowerCase().includes('session')) {
        next.status = status.toLowerCase()
      } else {
        const sessionStatus: Record<string, string> = {
          'session.created': 'pending',
          'session.started': 'running',
          'session.paused': 'paused',
          'session.resumed': 'running',
          'session.stopped': 'stopped',
          'session.completed': 'completed',
          'session.error': 'failed',
        }
        const nextStatus = sessionStatus[event.type]
        if (nextStatus) next.status = nextStatus
      }
      sessions.value = [next, ...sessions.value.filter((item) => item.id !== next.id)]
      if (activeSession.value?.id === next.id) activeSession.value = next
    }
    scheduleSessionRefresh(sessionId)
  }

  function startRealtime(): void {
    socket?.close()
    socket = new ObservationSocket({
      afterSeq: () => latestSeq.value,
      onEvent: acceptEvent,
      onStatus: (status) => {
        realtimeStatus.value = status
      },
      onReplayUnavailable: () => void loadBootstrap({ preserveDraft: true }),
    })
    socket.connect()
  }

  function stopRealtime(): void {
    socket?.close()
    socket = undefined
  }

  function clearActionError(): void {
    actionError.value = null
  }

  return {
    loading,
    initialized,
    error,
    actionError,
    actionPending,
    capabilities,
    config,
    savedConfig,
    agents,
    sessions,
    activeSession,
    selectedSessionId,
    environment,
    environments,
    mockConfig,
    events,
    latestSeq,
    realtimeStatus,
    selectedSession,
    active,
    allTransfers,
    registeredReady,
    configDirty,
    uiExtensions,
    supports,
    applyBootstrap,
    loadBootstrap,
    selectSession,
    refreshSession,
    saveConfig,
    restoreConfig,
    createSession,
    commandSession,
    saveEnvironment,
    reloadMock,
    saveMock,
    exportCsv,
    acceptEvent,
    startRealtime,
    stopRealtime,
    clearActionError,
  }
})
