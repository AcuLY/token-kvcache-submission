export function formatDateTime(value?: string): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(date)
}

export function formatClock(value?: string): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(date)
}

export function formatDuration(value?: number): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—'
  if (value < 1000) return `${value.toFixed(value < 10 ? 1 : 0)} ms`
  return `${(value / 1000).toFixed(2)} s`
}

export function formatBytes(value?: number): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—'
  const units = ['B', 'KB', 'MB', 'GB']
  let amount = value
  let unit = 0
  while (amount >= 1024 && unit < units.length - 1) {
    amount /= 1024
    unit += 1
  }
  return `${amount.toFixed(unit === 0 ? 0 : 2)} ${units[unit]}`
}

export function formatNumber(value?: number): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—'
  return new Intl.NumberFormat('zh-CN').format(value)
}

export function sessionStatusLabel(status?: string): string {
  const labels: Record<string, string> = {
    pending: '待开始',
    running: '进行中',
    paused: '已暂停',
    completed: '已完成',
    stopped: '已停止',
    failed: '失败',
  }
  return labels[String(status || '').toLowerCase()] || status || '未知'
}

export function eventTypeLabel(type: string): string {
  return type
    .replaceAll('_', ' ')
    .replaceAll('.', ' · ')
    .replace(/\b\w/g, (letter: string) => letter.toUpperCase())
}

export function modeLabel(mode?: string, variant?: string): string {
  const raw = [mode, variant].filter(Boolean).join(' / ').replaceAll('_', ' ').toUpperCase()
  return raw || '—'
}

export function directionLabel(source?: string, target?: string, direction?: string): string {
  if (source && target) return `${shortAgent(source)} → ${shortAgent(target)}`
  if (direction) return direction.replace(/_TO_/i, ' → ').replace(/->/g, ' → ')
  return '—'
}

export function shortAgent(value: string): string {
  const normalized = value.toLowerCase()
  if (normalized.endsWith('-a') || normalized === 'a' || normalized.includes('agent_a')) return 'A'
  if (normalized.endsWith('-b') || normalized === 'b' || normalized.includes('agent_b')) return 'B'
  return value
}

export function cloneData<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}
