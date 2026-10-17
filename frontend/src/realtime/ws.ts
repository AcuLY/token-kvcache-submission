import type { ObservationEvent } from '@/types/contracts'
import { normalizeEvent } from '@/types/normalizers'

export type RealtimeStatus = 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'closed'

interface RealtimeCallbacks {
  afterSeq: () => number
  onEvent: (event: ObservationEvent) => void
  onStatus: (status: RealtimeStatus) => void
  onReplayUnavailable: () => void
}

const backoff = [700, 1400, 2800, 5000, 8000]

function websocketUrl(afterSeq: number): string {
  const configured = import.meta.env.VITE_WS_URL
  if (configured) {
    const url = new URL(configured)
    url.searchParams.set('after_seq', String(afterSeq))
    return url.toString()
  }
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const url = new URL(`${protocol}//${window.location.host}/api/v1/observations`)
  url.searchParams.set('after_seq', String(afterSeq))
  return url.toString()
}

export class ObservationSocket {
  private socket?: WebSocket
  private timer?: number
  private attempts = 0
  private stopped = false

  constructor(private readonly callbacks: RealtimeCallbacks) {}

  connect(): void {
    this.stopped = false
    this.open()
  }

  close(): void {
    this.stopped = true
    if (this.timer) window.clearTimeout(this.timer)
    this.socket?.close()
    this.callbacks.onStatus('closed')
  }

  private open(): void {
    if (this.stopped) return
    this.callbacks.onStatus(this.attempts ? 'reconnecting' : 'connecting')
    const socket = new WebSocket(websocketUrl(this.callbacks.afterSeq()))
    this.socket = socket

    socket.onopen = () => {
      this.attempts = 0
      this.callbacks.onStatus('connected')
    }
    socket.onmessage = (message) => {
      let decoded: unknown
      try {
        decoded = JSON.parse(String(message.data))
      } catch {
        return
      }
      if (decoded && typeof decoded === 'object') {
        const envelope = decoded as Record<string, unknown>
        if (envelope.type === 'replay_unavailable' || envelope.type === 'resync_required' || envelope.resync_required === true) {
          this.callbacks.onReplayUnavailable()
          return
        }
        const events = Array.isArray(envelope.events) ? envelope.events : [decoded]
        events.forEach((item) => {
          const event = normalizeEvent(item)
          if (event) this.callbacks.onEvent(event)
        })
      }
    }
    socket.onclose = () => this.scheduleReconnect()
    socket.onerror = () => socket.close()
  }

  private scheduleReconnect(): void {
    if (this.stopped) return
    this.callbacks.onStatus('reconnecting')
    const delay = backoff[Math.min(this.attempts, backoff.length - 1)] ?? 8000
    this.attempts += 1
    this.timer = window.setTimeout(() => this.open(), delay)
  }
}
