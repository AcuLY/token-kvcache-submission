<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import {
  ChevronRight,
  CirclePause,
  CirclePlay,
  CircleStop,
  Clock3,
  ListRestart,
} from 'lucide-vue-next'
import { NButton, NIcon } from 'naive-ui'
import SessionSidebar from '@/components/SessionSidebar.vue'
import ConversationPanel from '@/components/ConversationPanel.vue'
import CurrentHandoffPanel from '@/components/CurrentHandoffPanel.vue'
import NetworkChart from '@/components/NetworkChart.vue'
import ObservationEventList from '@/components/ObservationEventList.vue'
import EmptyState from '@/components/EmptyState.vue'
import StatusDot from '@/components/StatusDot.vue'
import { useAppStore } from '@/stores/app'
import { formatDuration, formatNumber, modeLabel, sessionStatusLabel, shortAgent } from '@/utils/format'

const store = useAppStore()

const session = computed(() => store.selectedSession)
const config = computed(() => session.value?.config || store.config || undefined)
const transfers = computed(() => session.value?.transfers || session.value?.handoffs || [])
const inspectedTransferId = ref<string | null>(null)
const reviewElement = ref<HTMLElement | null>(null)
function inspectTransfer(transferId: string): void {
  inspectedTransferId.value = transferId
  reviewElement.value?.scrollIntoView({ block: 'start' })
}
watch(() => session.value?.id, () => { inspectedTransferId.value = null })
const currentTransfer = computed(() =>
  transfers.value.find((item) => item.transfer_id === inspectedTransferId.value) || transfers.value.at(-1),
)
const inspectedIndex = computed(() => transfers.value.findIndex((item) => item.transfer_id === currentTransfer.value?.transfer_id))
const sessionEvents = computed(() => {
  if (!session.value) return []
  return store.events.filter(
    (event) => (event.conversation_id || event.session_id) === session.value?.id,
  )
})
const latestTimings = computed(() => currentTransfer.value?.timings)
const canCreate = computed(() => Boolean(store.config) && store.registeredReady >= 2 && !store.active)

const statusTone = computed(() => {
  if (session.value?.status === 'running') return 'primary' as const
  if (session.value?.status === 'paused') return 'warning' as const
  if (session.value?.status === 'completed') return 'success' as const
  if (session.value?.status === 'failed') return 'danger' as const
  return 'muted' as const
})

function modePolicyLabel(value?: string): string {
  if (value === 'always_token') return '始终 Token'
  if (value === 'adaptive') return '自适应 Token / KV'
  if (value === 'always_kv') return '始终 KV（优先增量）'
  return value || '—'
}

async function createSession(): Promise<void> {
  await store.createSession()
}
</script>

<template>
  <div class="conversation-layout">
    <SessionSidebar
      :sessions="store.sessions"
      :agents="store.agents"
      :selected-id="store.selectedSessionId"
      :create-disabled="!canCreate"
      :create-loading="store.actionPending === 'create-session'"
      @select="store.selectSession"
      @create="createSession"
    />

    <div class="conversation-main">
      <section v-if="session" class="surface session-overview">
        <div class="session-overview__top">
          <div class="session-title-group">
            <div class="session-title-line">
              <h1>{{ session.title || config?.initial_input || `会话 ${session.id}` }}</h1>
              <span class="session-status"><StatusDot :tone="statusTone" :pulse="session.status === 'running'" />{{ sessionStatusLabel(session.status) }}</span>
            </div>
            <p>会话 ID：<span class="mono">{{ session.id }}</span></p>
          </div>
          <RouterLink to="/agents" class="config-link">修改配置 <ChevronRight :size="17" /></RouterLink>
        </div>
        <div class="config-summary">
          <span><strong>本会话配置</strong></span>
          <span>{{ config?.agent_a_profile.display_name || 'Agent A' }} ↔ {{ config?.agent_b_profile.display_name || 'Agent B' }}</span>
          <span>首位发言：{{ config?.first_speaker || '—' }}</span>
          <span>最大发言：{{ formatNumber(config?.max_agent_turns) }}</span>
          <span>上下文：{{ formatNumber(config?.max_context_tokens) }}</span>
          <span>策略：{{ modePolicyLabel(config?.mode_policy) }}</span>
          <span v-if="session.initial_state">公共 P₀：{{ formatNumber(session.initial_state.known_token_count) }} tokens</span>
        </div>
        <p
          v-if="store.savedConfig && config?.mode_policy !== store.savedConfig.mode_policy"
          class="next-session-policy"
        >
          新会话将使用：{{ modePolicyLabel(store.savedConfig.mode_policy) }}。点击“新会话并开始”生效；本会话保留创建时的策略。
        </p>
        <div class="session-actions">
          <NButton
            type="primary"
            secondary
            :disabled="!canCreate"
            :loading="store.actionPending === 'create-session'"
            @click="createSession"
          >
            <template #icon><NIcon><ListRestart /></NIcon></template>
            新会话并开始
          </NButton>
          <NButton
            :disabled="session.status !== 'running' || store.activeSession?.id !== session.id"
            :loading="store.actionPending === 'session-pause'"
            @click="store.commandSession('pause')"
          >
            <template #icon><NIcon><CirclePause /></NIcon></template>
            暂停
          </NButton>
          <NButton
            :disabled="session.status !== 'paused' || store.activeSession?.id !== session.id"
            :loading="store.actionPending === 'session-resume'"
            @click="store.commandSession('resume')"
          >
            <template #icon><NIcon><CirclePlay /></NIcon></template>
            继续
          </NButton>
          <NButton
            type="error"
            ghost
            :disabled="!['pending', 'running', 'paused'].includes(session.status) || store.activeSession?.id !== session.id"
            :loading="store.actionPending === 'session-stop'"
            @click="store.commandSession('stop')"
          >
            <template #icon><NIcon><CircleStop /></NIcon></template>
            停止
          </NButton>
        </div>
      </section>

      <section v-else class="surface no-session">
        <EmptyState
          title="还没有可查看的会话"
          :description="store.registeredReady < 2 ? '等待两个 Agent 完成注册并进入 READY。' : '在已保存配置就绪后，可从左侧新建并直接开始会话。'"
        >
          <NButton type="primary" :disabled="!canCreate" @click="createSession">新会话并开始</NButton>
        </EmptyState>
      </section>

      <ConversationPanel
        v-if="session"
        :messages="session.messages || []"
        :config="config"
        :running="session.status === 'running'"
        :transfers="transfers"
        @select-transfer="inspectTransfer"
      />

      <section v-if="session" class="observation-section" aria-labelledby="observation-heading">
        <h2 id="observation-heading"><span></span>实时观测</h2>

        <div ref="reviewElement" class="transfer-review">
          <div class="review-toolbar">
            <div>
              <strong>交接回看</strong>
              <span v-if="transfers.length">第 {{ inspectedIndex + 1 }} / {{ transfers.length }} 次 · {{ inspectedTransferId ? '已固定查看' : '跟随最新' }}</span>
              <span v-else>交接完成后仍可逐次查看</span>
            </div>
            <NButton size="small" :disabled="!inspectedTransferId" @click="inspectedTransferId = null">跟随最新</NButton>
          </div>
          <div v-if="transfers.length" class="transfer-history" role="group" aria-label="选择交接记录">
            <button
              v-for="(item, index) in transfers"
              :key="item.transfer_id"
              type="button"
              :aria-pressed="currentTransfer?.transfer_id === item.transfer_id"
              @click="inspectedTransferId = item.transfer_id"
            >
              <strong>第 {{ index + 1 }} 次 · {{ modeLabel(item.mode, item.variant) }}</strong>
              <span>{{ shortAgent(item.source_agent_id || '—') }} → {{ shortAgent(item.target_agent_id || '—') }}</span>
            </button>
          </div>
          <CurrentHandoffPanel :transfer="currentTransfer" heading="交接详情" />
        </div>

        <section class="surface network-observation">
          <div class="surface-header">
            <div>
              <h3 class="surface-title">网络传输</h3>
              <p>来自交接计划绑定的版本化网络快照</p>
            </div>
            <span class="realtime-pill"><StatusDot tone="primary" :pulse="session.status === 'running'" />实时</span>
          </div>
          <NetworkChart :transfers="transfers" :events="sessionEvents" :height="250" />
        </section>

        <div class="observation-bottom">
          <section class="surface phase-panel">
            <div class="surface-header">
              <h3 class="surface-title">阶段耗时</h3>
              <span v-if="currentTransfer" class="muted">第 {{ inspectedIndex + 1 }} 次交接</span>
            </div>
            <EmptyState v-if="!latestTimings" compact title="暂无阶段耗时" description="成功交接后显示各阶段实测值。" />
            <dl v-else class="phase-grid">
              <div><dt>准备</dt><dd>{{ formatDuration(latestTimings.sender_prepare_ms ?? latestTimings.prepare_ms) }}</dd></div>
              <div><dt>P2P 传输</dt><dd>{{ formatDuration(latestTimings.p2p_ms) }}</dd></div>
              <div><dt>{{ currentTransfer?.mode === 'KV' ? 'KV 导入' : '接收端 Prefill' }}</dt><dd>{{ formatDuration(currentTransfer?.mode === 'KV' ? latestTimings.receiver_import_ms : latestTimings.receiver_prefill_ms) }}</dd></div>
              <div><dt>本地 Prefill</dt><dd>{{ formatDuration(latestTimings.local_prefill_ms) }}</dd></div>
              <div><dt>生成</dt><dd>{{ formatDuration(latestTimings.generation_ms) }}</dd></div>
              <div class="phase-total"><dt><Clock3 :size="14" />总耗时</dt><dd>{{ formatDuration(latestTimings.total_ms) }}</dd></div>
            </dl>
          </section>

          <section class="surface events-panel">
            <div class="surface-header">
              <h3 class="surface-title">最新事件</h3>
              <span class="mono muted">seq {{ store.latestSeq }}</span>
            </div>
            <ObservationEventList :events="sessionEvents" :limit="6" />
          </section>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.conversation-layout {
  display: grid;
  min-height: calc(100vh - var(--header-height));
  grid-template-columns: 294px minmax(0, 1fr);
}

.conversation-main {
  display: flex;
  width: 100%;
  min-width: 0;
  max-width: 1450px;
  padding: 14px 20px 32px;
  margin: 0 auto;
  flex-direction: column;
  gap: 12px;
}

.session-overview { overflow: hidden; }
.transfer-review { min-width: 0; scroll-margin-top: calc(var(--header-height) + 12px); }
.review-toolbar { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px; margin-bottom: 10px; }
.review-toolbar > div { display: flex; align-items: baseline; flex-wrap: wrap; gap: 12px; }
.review-toolbar strong { font-size: 14px; }
.review-toolbar span { font-size: 12px; color: var(--color-text-secondary); }
.transfer-history { display: flex; flex-wrap: wrap; gap: 8px; padding-bottom: 10px; }
.transfer-history button { display: grid; gap: 5px; flex: 0 0 auto; padding: 10px 14px; border: 1px solid var(--color-border); border-radius: 6px; background: var(--color-surface); color: var(--color-text-secondary); cursor: pointer; text-align: left; }
.transfer-history button strong { font-size: 12px; }
.transfer-history button span { font-size: 11px; }
.transfer-history button[aria-pressed='true'] { border-color: var(--color-primary); background: var(--color-primary-soft); color: var(--color-primary); }
.transfer-history button:focus-visible { outline: 2px solid var(--color-primary); outline-offset: -3px; }
.next-session-policy { margin: 12px 20px 0; color: var(--color-text-secondary); font-size: 12px; line-height: 1.6; }

.session-overview__top {
  display: flex;
  padding: 17px 20px 13px;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
}

.session-title-line {
  display: flex;
  align-items: center;
  gap: 13px;
}

.session-title-group h1 {
  max-width: 900px;
  margin: 0;
  color: var(--color-text);
  font-size: 20px;
  line-height: 1.35;
}

.session-title-group p {
  margin: 5px 0 0;
  color: var(--color-text-muted);
  font-size: 11px;
}

.session-status,
.realtime-pill {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  color: var(--color-text-secondary);
  font-size: 12px;
  white-space: nowrap;
}

.config-link {
  display: inline-flex;
  min-height: 32px;
  align-items: center;
  color: var(--color-primary);
  font-size: 13px;
  font-weight: 600;
}

.config-summary {
  display: flex;
  min-height: 40px;
  padding: 9px 20px;
  align-items: center;
  gap: 0;
  overflow-x: auto;
  color: var(--color-text-secondary);
  border-top: 1px solid var(--color-border-soft);
  border-bottom: 1px solid var(--color-border-soft);
  font-size: 12px;
  white-space: nowrap;
}

.config-summary span {
  padding: 0 15px;
  border-left: 1px solid var(--color-border);
}

.config-summary span:first-child { padding-left: 0; border-left: 0; }

.session-actions {
  display: flex;
  padding: 11px 20px;
  gap: 10px;
}

.no-session { min-height: 260px; }

.observation-section {
  display: flex;
  margin-top: 3px;
  flex-direction: column;
  gap: 12px;
}

.observation-section > h2 {
  display: flex;
  margin: 2px 0 0;
  align-items: center;
  gap: 10px;
  font-size: 20px;
}

.observation-section > h2 > span {
  width: 4px;
  height: 23px;
  background: var(--color-primary);
  border-radius: 3px;
}

.network-observation .surface-header p {
  margin: 4px 0 0;
  color: var(--color-text-muted);
  font-size: 11px;
}

.observation-bottom {
  display: grid;
  align-items: stretch;
  grid-template-columns: minmax(420px, 0.9fr) minmax(480px, 1.1fr);
  gap: 12px;
}

.phase-grid {
  display: grid;
  padding: 10px 16px 14px;
  margin: 0;
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.phase-grid > div { padding: 12px; }
.phase-grid > div:nth-child(n + 4) { border-top: 1px solid var(--color-border-soft); }
.phase-grid > div:not(:nth-child(3n + 1)) { border-left: 1px solid var(--color-border-soft); }
.phase-grid dt { display: flex; align-items: center; gap: 5px; color: var(--color-text-muted); font-size: 11px; }
.phase-grid dd { margin: 5px 0 0; color: var(--color-text); font-size: 15px; font-weight: 650; font-variant-numeric: tabular-nums; }
.phase-total dd { color: var(--color-primary); }
.events-panel { overflow: hidden; }

@media (max-width: 1180px) {
  .observation-bottom { grid-template-columns: 1fr; }
}

@media (max-width: 1020px) {
  .conversation-layout { grid-template-columns: minmax(0, 1fr); }
  .conversation-main { padding-top: 16px; }
}

@media (max-width: 680px) {
  .conversation-main { padding: 12px; }
  .session-overview__top { padding: 14px; }
  .session-title-line { align-items: flex-start; flex-direction: column; gap: 7px; }
  .session-title-group h1 { font-size: 17px; }
  .config-link { display: none; }
  .config-summary { padding-inline: 14px; }
  .session-actions { display: grid; padding: 10px 14px 14px; grid-template-columns: 1fr 1fr; }
  .session-actions :deep(.n-button:first-child) { grid-column: 1 / -1; }
  .phase-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .phase-grid > div:nth-child(n + 4) { border-top: 0; }
  .phase-grid > div:not(:nth-child(3n + 1)) { border-left: 0; }
  .phase-grid > div:nth-child(n + 3) { border-top: 1px solid var(--color-border-soft); }
  .phase-grid > div:nth-child(even) { border-left: 1px solid var(--color-border-soft); }
}
</style>
