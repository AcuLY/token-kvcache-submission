<script setup lang="ts">
import { computed } from 'vue'
import { Bot, Plus } from 'lucide-vue-next'
import { NButton, NIcon } from 'naive-ui'
import type { AgentRegistration, SessionSummary } from '@/types/contracts'
import StatusDot from './StatusDot.vue'
import { formatClock, formatDateTime, sessionStatusLabel } from '@/utils/format'

const props = defineProps<{
  sessions: SessionSummary[]
  selectedId?: string | null
  agents: AgentRegistration[]
  createDisabled?: boolean
  createLoading?: boolean
}>()

const emit = defineEmits<{
  select: [id: string]
  create: []
}>()

const activeSessions = computed(() =>
  props.sessions.filter((item) => ['pending', 'running', 'paused'].includes(item.status)),
)
const historicSessions = computed(() =>
  props.sessions.filter((item) => !['pending', 'running', 'paused'].includes(item.status)),
)

function statusTone(status?: string): 'success' | 'primary' | 'warning' | 'danger' | 'muted' {
  if (status === 'running') return 'primary'
  if (status === 'paused') return 'warning'
  if (status === 'completed') return 'success'
  if (status === 'failed') return 'danger'
  return 'muted'
}

function online(agent: AgentRegistration): boolean {
  if (typeof agent.online === 'boolean') return agent.online
  return ['ready', 'busy', 'online', 'healthy'].includes(String(agent.status || '').toLowerCase())
}
</script>

<template>
  <aside class="session-sidebar" aria-label="会话记录">
    <div class="sidebar-main">
      <h2>会话记录</h2>
      <NButton
        type="primary"
        block
        :loading="createLoading"
        :disabled="createDisabled"
        @click="emit('create')"
      >
        <template #icon><NIcon><Plus /></NIcon></template>
        新会话并开始
      </NButton>

      <div class="session-groups">
        <section v-if="activeSessions.length" class="session-group">
          <h3>进行中</h3>
          <button
            v-for="session in activeSessions"
            :key="session.id"
            class="session-row"
            :class="{ 'session-row--selected': selectedId === session.id }"
            type="button"
            @click="emit('select', session.id)"
          >
            <strong class="truncate">{{ session.title || session.config?.initial_input || session.id }}</strong>
            <span class="session-row__meta">
              <span><StatusDot :tone="statusTone(session.status)" />{{ sessionStatusLabel(session.status) }}</span>
              <time>{{ formatClock(session.updated_at || session.started_at || session.created_at) }}</time>
            </span>
          </button>
        </section>

        <section v-if="historicSessions.length" class="session-group">
          <h3>历史</h3>
          <button
            v-for="session in historicSessions"
            :key="session.id"
            class="session-row"
            :class="{ 'session-row--selected': selectedId === session.id }"
            type="button"
            @click="emit('select', session.id)"
          >
            <strong class="truncate">{{ session.title || session.config?.initial_input || session.id }}</strong>
            <span class="session-row__meta">
              <span><StatusDot :tone="statusTone(session.status)" />{{ sessionStatusLabel(session.status) }}</span>
              <time>{{ formatDateTime(session.updated_at || session.created_at) }}</time>
            </span>
          </button>
        </section>

        <p v-if="!sessions.length" class="sessions-empty">尚无会话记录</p>
      </div>
    </div>

    <div class="agent-mini-list">
      <div v-if="!agents.length" class="agents-empty">等待 Agent 注册…</div>
      <div v-for="(agent, index) in agents" :key="agent.agent_id" class="agent-mini">
        <span class="agent-avatar" :class="`agent-avatar--${index % 2 ? 'b' : 'a'}`">
          <Bot :size="16" :stroke-width="2" />
        </span>
        <span class="agent-mini__identity">
          <strong>{{ agent.display_name || agent.agent_id }}</strong>
          <small class="truncate">{{ agent.runtime?.model_id || agent.runtime_descriptor?.model_id || '运行时待上报' }}</small>
        </span>
        <span class="agent-mini__status"><StatusDot :tone="online(agent) ? 'success' : 'muted'" />{{ online(agent) ? (String(agent.status).toLowerCase() === 'busy' ? '工作中' : '在线') : '离线' }}</span>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.session-sidebar {
  position: sticky;
  top: var(--header-height);
  display: flex;
  height: calc(100vh - var(--header-height));
  min-width: 0;
  flex-direction: column;
  background: #fff;
  border-right: 1px solid var(--color-border);
}

.sidebar-main {
  min-height: 0;
  padding: 22px 18px 16px;
  overflow-y: auto;
}

h2 {
  margin: 0 0 16px;
  color: var(--color-text);
  font-size: 18px;
  font-weight: 700;
}

.session-groups {
  margin-top: 24px;
}

.session-group + .session-group { margin-top: 20px; }

h3 {
  margin: 0 0 8px;
  color: var(--color-text-secondary);
  font-size: 13px;
  font-weight: 600;
}

.session-row {
  display: flex;
  width: 100%;
  min-height: 66px;
  padding: 12px 13px;
  flex-direction: column;
  gap: 9px;
  color: var(--color-text);
  text-align: left;
  cursor: pointer;
  background: #fff;
  border: 1px solid transparent;
  border-bottom-color: var(--color-border-soft);
  border-radius: 6px;
}

.session-row:hover { background: #f8faff; }

.session-row--selected {
  background: var(--color-primary-soft);
  border-color: #9dbbff;
  box-shadow: inset 3px 0 var(--color-primary);
}

.session-row strong {
  max-width: 100%;
  font-size: 13px;
  font-weight: 600;
}

.session-row__meta,
.session-row__meta > span {
  display: flex;
  align-items: center;
}

.session-row__meta {
  justify-content: space-between;
  gap: 8px;
  color: var(--color-text-muted);
  font-size: 12px;
}

.session-row__meta > span { gap: 6px; }

.sessions-empty,
.agents-empty {
  margin: 24px 0;
  color: var(--color-text-muted);
  font-size: 13px;
  text-align: center;
}

.agent-mini-list {
  flex: 0 0 auto;
  margin: 14px 18px 18px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  overflow: hidden;
}

.agent-mini {
  display: grid;
  min-height: 58px;
  padding: 10px 12px;
  align-items: center;
  grid-template-columns: auto minmax(0, 1fr) auto;
  gap: 9px;
}

.agent-mini + .agent-mini { border-top: 1px solid var(--color-border-soft); }

.agent-avatar {
  display: grid;
  width: 29px;
  height: 29px;
  color: #fff;
  border-radius: 50%;
  place-items: center;
}

.agent-avatar--a { background: var(--color-cyan); }
.agent-avatar--b { background: var(--color-orange); }

.agent-mini__identity {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 2px;
}

.agent-mini__identity strong { font-size: 13px; }
.agent-mini__identity small { color: var(--color-text-muted); font-size: 10px; }

.agent-mini__status {
  display: flex;
  align-items: center;
  gap: 5px;
  color: var(--color-text-secondary);
  font-size: 10px;
}

@media (max-width: 1020px) {
  .session-sidebar {
    position: static;
    height: auto;
    border-right: 0;
    border-bottom: 1px solid var(--color-border);
  }
  .sidebar-main { padding: 14px 16px; overflow: visible; }
  .sidebar-main h2 { display: none; }
  .session-groups { display: flex; margin-top: 14px; overflow-x: auto; gap: 10px; }
  .session-group { display: contents; }
  .session-group h3 { display: none; }
  .session-row { flex: 0 0 220px; }
  .agent-mini-list { display: none; }
}
</style>
