<script setup lang="ts">
import { computed, ref } from 'vue'
import { Bot, ChevronRight, Copy, X } from 'lucide-vue-next'
import { NButton, NIcon, NModal, NTag, useMessage } from 'naive-ui'
import type { AgentRegistration } from '@/types/contracts'
import StatusDot from './StatusDot.vue'
import { formatDateTime } from '@/utils/format'

const props = withDefaults(defineProps<{ agent: AgentRegistration; accent?: 'a' | 'b' }>(), { accent: 'a' })
const showRaw = ref(false)
const message = useMessage()

const runtime = computed(() => props.agent.runtime_descriptor || props.agent.runtime || {})
const capabilities = computed(() => [
  ...(props.agent.runtime_capabilities || []),
  ...(props.agent.transfer_capabilities || []),
  ...(props.agent.transport_capabilities || []),
  ...(runtime.value.capabilities || []),
].filter((item, index, list) => list.indexOf(item) === index))

const ready = computed(() => {
  if (typeof props.agent.ready === 'boolean') return props.agent.ready
  return ['ready', 'online', 'healthy'].includes(String(props.agent.status || '').toLowerCase())
})

async function copy(value?: string): Promise<void> {
  if (!value) return
  await navigator.clipboard.writeText(value)
  message.success('已复制')
}
</script>

<template>
  <article class="registration-card" :class="`registration-card--${accent}`">
    <header>
      <span class="agent-symbol"><Bot :size="19" /></span>
      <h3>{{ agent.display_name || agent.agent_id }}</h3>
      <span class="ready-status"><StatusDot :tone="ready ? 'success' : 'muted'" />{{ ready ? 'READY' : agent.status || 'OFFLINE' }}</span>
      <button type="button" class="raw-link" @click="showRaw = true">查看原始注册信息 <ChevronRight :size="15" /></button>
    </header>
    <dl>
      <div><dt>实例 ID</dt><dd class="mono">{{ agent.instance_id || '—' }}</dd></div>
      <div><dt>最后心跳</dt><dd>{{ formatDateTime(agent.last_heartbeat_at) }}</dd></div>
      <div>
        <dt>控制端点</dt>
        <dd class="endpoint"><span class="truncate">{{ agent.control_url || agent.control_endpoint || '—' }}</span><button v-if="agent.control_url || agent.control_endpoint" type="button" aria-label="复制控制端点" @click="copy(agent.control_url || agent.control_endpoint)"><Copy :size="13" /></button></dd>
      </div>
      <div><dt>P2P 绑定端点</dt><dd class="mono">{{ agent.data_bind || agent.p2p_bind_endpoint || '—' }}</dd></div>
      <div><dt>P2P 公告端点</dt><dd class="mono">{{ agent.data_advertise || agent.p2p_advertise_endpoint || '—' }}</dd></div>
      <div><dt>运行时描述</dt><dd>{{ runtime.model_id || runtime.runtime_id || runtime.provider || '—' }}<span v-if="runtime.revision" class="muted"> / {{ runtime.revision }}</span></dd></div>
      <div class="capability-row"><dt>能力</dt><dd><NTag v-for="item in capabilities" :key="item" size="small" :bordered="true">{{ item }}</NTag><span v-if="!capabilities.length">—</span></dd></div>
    </dl>

    <NModal v-model:show="showRaw" preset="card" :title="`${agent.agent_id} 原始注册信息`" class="raw-modal" :style="{ width: 'min(760px, calc(100vw - 32px))' }">
      <template #header-extra>
        <NButton quaternary circle aria-label="关闭" @click="showRaw = false"><template #icon><NIcon><X /></NIcon></template></NButton>
      </template>
      <pre>{{ JSON.stringify(agent, null, 2) }}</pre>
    </NModal>
  </article>
</template>

<style scoped>
.registration-card { padding: 15px 18px 16px; border: 1px solid var(--color-border); border-left: 3px solid; border-radius: 7px; }
.registration-card--a { border-left-color: var(--color-cyan); }
.registration-card--b { border-left-color: var(--color-orange); }
header { display: flex; align-items: center; gap: 9px; }
.agent-symbol { display: grid; width: 34px; height: 34px; color: #fff; border-radius: 50%; place-items: center; }
.registration-card--a .agent-symbol { background: var(--color-cyan); }
.registration-card--b .agent-symbol { background: var(--color-orange); }
h3 { margin: 0; font-size: 16px; }
.ready-status { display: inline-flex; align-items: center; gap: 6px; color: var(--color-success); font-size: 12px; }
.raw-link { display: inline-flex; margin-left: auto; padding: 6px 0; align-items: center; color: var(--color-primary); cursor: pointer; background: none; border: 0; font-size: 12px; }
dl { display: grid; margin: 15px 0 0; grid-template-columns: 1fr 1fr; gap: 8px 18px; }
dl > div { display: grid; min-width: 0; grid-template-columns: 95px minmax(0, 1fr); gap: 8px; }
dt { color: var(--color-text-secondary); font-size: 11px; font-weight: 600; }
dd { min-width: 0; margin: 0; color: #4a5874; font-size: 11px; overflow-wrap: anywhere; }
.endpoint { display: flex; align-items: center; gap: 6px; color: var(--color-primary); }
.endpoint button { display: inline-grid; padding: 2px; color: var(--color-primary); cursor: pointer; background: transparent; border: 0; place-items: center; }
.capability-row { grid-column: 1 / -1; }
.capability-row dd { display: flex; flex-wrap: wrap; gap: 5px; }
pre { max-height: 60vh; padding: 14px; margin: 0; overflow: auto; color: #dce7ff; background: #091630; border-radius: 6px; font: 12px/1.6 "SFMono-Regular", Consolas, monospace; }
@media (max-width: 640px) {
  header { flex-wrap: wrap; }
  .raw-link { width: 100%; margin-left: 43px; }
  dl { grid-template-columns: 1fr; }
  .capability-row { grid-column: auto; }
}
</style>
