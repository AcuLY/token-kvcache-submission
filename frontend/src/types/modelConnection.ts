export interface ModelConnectionConfig {
  mode: 'http' | 'ssh'
  url: string
  ssh_host: string
  remote_port: number
  source_address: string
  interface_index: number
}
export interface ModelDescriptor {
  model_id: string
  dtype: string
  provider: string
}
export interface ModelConnectionStatus {
  url: string
  config: ModelConnectionConfig
  switching: boolean
  progress?: { attempt: number; max_attempts: number; next_retry_at: number | null; error: string | null } | null
  active_session: boolean
  connected: boolean
  error: string | null
  descriptor?: ModelDescriptor
}
export interface LocalInterface {
  InterfaceAlias: string
  InterfaceIndex: number
  IPAddress: string
}
