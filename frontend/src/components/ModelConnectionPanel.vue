<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { NButton, NInput, NInputNumber, NSelect } from 'naive-ui'
import { api } from '@/api/http'
import { useAppStore } from '@/stores/app'
import type { LocalInterface, ModelConnectionConfig, ModelConnectionStatus } from '@/types/modelConnection'

const store = useAppStore()
const state = ref<ModelConnectionStatus | null>(null)
const interfaces = ref<LocalInterface[]>([])
const busy = ref('')
const error = ref('')
const notice = ref('')
const now = ref(Date.now())
const locked = computed(() => Boolean(busy.value) || Boolean(state.value?.switching))
const retrySeconds = computed(() => Math.max(0, Math.ceil(((state.value?.progress?.next_retry_at || 0) * 1000 - now.value) / 1000)))
let polling = false
const timer = window.setInterval(async () => {
  now.value = Date.now()
  if (polling) return
  polling = true
  try { state.value = await api.modelConnection() } catch { /* Keep the last known state during transient failures. */ }
  finally { polling = false }
}, 1000)
onUnmounted(() => window.clearInterval(timer))
const draft = ref<ModelConnectionConfig>({ mode: 'http', url: 'http://127.0.0.1:18081', ssh_host: 'token-kv', remote_port: 18100, source_address: '', interface_index: 0 })
const networkOptions = computed(() => [
  { label: '自动选择', value: '' },
  ...interfaces.value.map(i => ({ label: `${i.InterfaceAlias} · ${i.IPAddress}`, value: i.IPAddress })),
])
const hasActive = computed(() => Boolean(store.active || state.value?.active_session))
const valid = computed(() => draft.value.mode === 'http' ? /^https?:\/\//.test(draft.value.url) : Boolean(draft.value.ssh_host.trim() && draft.value.remote_port))

function network(value: string) {
  draft.value.source_address = value
  draft.value.interface_index = interfaces.value.find(i => i.IPAddress === value)?.InterfaceIndex || 0
}
async function refresh(initial = false) {
  busy.value = 'refresh'
  error.value = ''
  try {
    state.value = await api.modelConnection()
    if (initial) draft.value = { ...state.value.config }
  } catch (e) { error.value = e instanceof Error ? e.message : '无法获取连接状态' }
  finally { busy.value = '' }
}
async function connect(test = false) {
  busy.value = test ? 'test' : 'connect'
  error.value = ''
  notice.value = ''
  try {
    if (test) {
      const descriptor = await api.testModelConnection(draft.value)
      notice.value = `连接测试通过：${descriptor.model_id}。点击“连接并使用”后应用于新会话。`
    } else {
      state.value = await api.connectModel(draft.value)
      await store.loadBootstrap({ preserveDraft: true })
      notice.value = '两个 Agent 已切换，新会话将使用此模型。'
    }
  } catch (e) { error.value = e instanceof Error ? e.message : '连接失败' }
  finally { busy.value = '' }
}
onMounted(async () => {
  await refresh(true)
  try { interfaces.value = await api.modelInterfaces() } catch { /* Auto routing stays available. */ }
})
</script>

<template>
  <section class="surface model-connection" aria-label="模型连接">
    <header>
      <div><h2>模型连接</h2><p>连接已有模型服务；切换后保留提示词和历史记录。</p></div>
      <NButton :disabled="locked" @click="refresh()">刷新状态</NButton>
    </header>
    <p class="connection-state" role="status">
      {{ state?.connected ? '已连接' : state ? '未就绪' : '正在读取状态…' }}
      <template v-if="state?.descriptor"> · {{ state.descriptor.model_id }} · {{ state.descriptor.dtype }}</template>
    </p>
    <p v-if="state?.error" class="connection-error">{{ state.error }}</p>
    <div class="connection-fields">
      <label><span>连接方式</span><NSelect v-model:value="draft.mode" :disabled="locked" :options="[{ label: '本机或 HTTP 服务', value: 'http' }, { label: '实验室服务器 · SSH', value: 'ssh' }]" /></label>
      <label v-if="draft.mode === 'http'" class="wide"><span>模型服务地址</span><NInput v-model:value="draft.url" :disabled="locked" placeholder="http://127.0.0.1:18081" /></label>
      <template v-else>
        <label><span>SSH 主机</span><NInput v-model:value="draft.ssh_host" :disabled="locked" placeholder="token-kv 或 user@host" /></label>
        <label><span>服务器模型端口</span><NInputNumber v-model:value="draft.remote_port" :disabled="locked" :min="1" :max="65535" /></label>
        <label class="wide"><span>连接网卡</span><NSelect :value="draft.source_address" :disabled="locked" :options="networkOptions" @update:value="network" /></label>
      </template>
    </div>
    <p v-if="draft.mode === 'ssh'" class="help">使用本机已有的 SSH 配置和密钥。连接拒绝、中断或超时将自动重试，最多 4 次，间隔 5、15、30 秒。若代理影响内网连接，可选择 WLAN 网卡直连。</p>
    <p v-if="state?.switching && state.progress" role="status">
      第 {{ state.progress.attempt }} / {{ state.progress.max_attempts }} 次连接
      <template v-if="state.progress.next_retry_at"> · {{ retrySeconds }} 秒后重试</template>
      <template v-else> · 正在连接并检查模型服务…</template>
    </p>
    <p v-if="state?.switching && state.progress?.error" class="connection-error">上次尝试：{{ state.progress.error }}</p>
    <p v-if="hasActive" class="help">请先结束当前会话，再切换模型。</p>
    <p v-else-if="store.registeredReady < 2" class="help">等待两个 Agent 就绪后可切换模型。</p>
    <p v-if="error" role="alert" class="connection-error">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <footer>
      <NButton :disabled="!valid || locked" :loading="busy === 'test'" @click="connect(true)">测试连接</NButton>
      <NButton type="primary" :disabled="!valid || hasActive || store.registeredReady < 2 || locked" :loading="busy === 'connect'" @click="connect()">{{ busy === 'connect' ? '正在连接…' : '连接并使用' }}</NButton>
    </footer>
  </section>
</template>

<style scoped>
.model-connection { padding: 20px; margin-bottom: 18px; }
header { display: flex; justify-content: space-between; align-items: start; gap: 16px; }
h2 { margin: 0 0 8px; font-size: 18px; }
header p, .help { margin: 0; color: var(--color-text-secondary); font-size: 13px; line-height: 1.6; }
.connection-state { overflow-wrap: anywhere; font-size: 14px; }
.connection-fields { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; margin: 20px 0 12px; }
label { display: grid; gap: 8px; font-size: 13px; min-width: 0; }
.wide { grid-column: span 2; }
.connection-error { color: var(--color-danger); overflow-wrap: anywhere; }
footer { display: flex; justify-content: flex-end; gap: 12px; margin-top: 18px; }
:deep(.n-input-number) { width: 100%; }
@media (max-width: 750px) {
  .connection-fields { grid-template-columns: minmax(0, 1fr); }
  .wide { grid-column: auto; }
  header { flex-wrap: wrap; }
  footer { flex-wrap: wrap; }
}
</style>
