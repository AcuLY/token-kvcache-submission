<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { Bot, CheckCircle2, CircleMinus, Clock3, FileText, GitBranch, Layers, LoaderCircle } from 'lucide-vue-next'
import type { ConversationMessage, SessionConfig, TransferRecord } from '@/types/contracts'
import EmptyState from './EmptyState.vue'
import { formatClock, formatNumber, modeLabel } from '@/utils/format'

const props = defineProps<{
  messages: ConversationMessage[]
  config?: SessionConfig
  running?: boolean
  transfers?: TransferRecord[]
}>()
defineEmits<{ selectTransfer: [transferId: string] }>()

const messageRows = computed(() => {
  const byTurn = new Map((props.transfers || []).map(transfer => [
    `${transfer.source_agent_id}:${transfer.turn_id ?? transfer.turn_index}`, transfer,
  ]))
  return props.messages.map(message => ({
    message, transfer: byTurn.get(`${message.agent_id}:${message.turn_index}`),
  }))
})

function transferStatus(transfer: TransferRecord): string {
  const value = (transfer.status || '').toLowerCase()
  if (value === 'committed') return '已发送'
  if (value === 'failed') return '交接失败'
  return '交接中'
}

const viewport = ref<HTMLElement>()
const agentAId = computed(() => props.config?.agent_a_profile.agent_id)

function side(message: ConversationMessage): 'a' | 'b' {
  return message.agent_id === agentAId.value ? 'a' : 'b'
}

function name(message: ConversationMessage): string {
  if (message.display_name) return message.display_name
  if (side(message) === 'a') return props.config?.agent_a_profile.display_name || message.agent_id
  return props.config?.agent_b_profile.display_name || message.agent_id
}

watch(
  () => props.messages.length,
  async () => {
    await nextTick()
    if (viewport.value) viewport.value.scrollTop = viewport.value.scrollHeight
  },
)
</script>

<template>
  <section class="surface conversation-panel" aria-labelledby="conversation-heading">
    <div class="surface-header">
      <h2 id="conversation-heading" class="surface-title">对话</h2>
      <span v-if="messages.length" class="message-count">{{ messages.length }} 次发言</span>
    </div>
    <div ref="viewport" class="conversation-viewport">
      <EmptyState
        v-if="!messages.length"
        :title="running ? '等待首个 Agent 回复' : '尚无对话内容'"
        :description="running ? '会话已启动，生成完成后会自动显示在这里。' : '从左侧新建会话；初始任务与 Agent 配置会取自当前已保存配置。'"
      />
      <article
        v-for="{ message, transfer } in messageRows"
        v-else
        :key="message.id || `${message.agent_id}-${message.turn_index}`"
        class="message"
        :class="`message--${side(message)}`"
      >
        <span class="message-avatar" aria-hidden="true">
          <Bot :size="19" :stroke-width="2" />
        </span>
        <div class="message-body">
          <header>
            <strong>{{ name(message) }}</strong>
            <span>第 {{ message.turn_index }} 轮</span>
            <time>{{ formatClock(message.completed_at || message.created_at) }}</time>
            <span v-if="message.token_count !== undefined" class="message-tokens">
              {{ formatNumber(message.output_token_count ?? message.token_count) }} {{ message.output_token_count !== undefined ? '输出 tokens' : 'tokens' }}
            </span>
            <span v-if="message.status === 'generating'" class="message-state message-state--running">
              <LoaderCircle :size="15" class="spin" />生成中
            </span>
            <span v-else class="message-state message-state--done">
              <CheckCircle2 :size="15" />完成
            </span>
          </header>
          <p>{{ message.text }}</p>
          <div class="message-transfer">
            <button
              v-if="transfer"
              type="button"
              class="transfer-badge"
              :class="transfer.mode === 'TOKEN' ? 'transfer-badge--token' : transfer.variant === 'DELTA' ? 'transfer-badge--delta' : 'transfer-badge--full'"
              title="查看本次交接详情"
              @click="$emit('selectTransfer', transfer.transfer_id)"
            >
              <FileText v-if="transfer.mode === 'TOKEN'" :size="15" aria-hidden="true" />
              <GitBranch v-else-if="transfer.variant === 'DELTA'" :size="15" aria-hidden="true" />
              <Layers v-else :size="15" aria-hidden="true" />
              <span>{{ modeLabel(transfer.mode, transfer.variant) }}</span>
            </button>
            <span v-if="transfer" :class="{ 'transfer-failed': transfer.status?.toLowerCase() === 'failed' }">{{ transferStatus(transfer) }}</span>
            <span v-else class="transfer-none">
              <Clock3 v-if="running" :size="14" aria-hidden="true" /><CircleMinus v-else :size="14" aria-hidden="true" />
              {{ running ? '待交接' : '本轮无交接' }}
            </span>
          </div>
        </div>
      </article>
    </div>
  </section>
</template>

<style scoped>
.conversation-panel { min-height: 360px; }

.message-count {
  color: var(--color-text-muted);
  font-size: 12px;
}
.message-transfer { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin-top: 12px; color: var(--color-text-secondary); font-size: 11px; }
.transfer-badge { display: inline-flex; align-items: center; gap: 6px; padding: 4px 8px; border: 1px solid currentColor; border-radius: 5px; font: inherit; font-size: 11px; font-weight: 650; cursor: pointer; }
.transfer-badge svg { flex-shrink: 0; }
.transfer-badge--token { color: #245bb8; background: #edf3ff; }
.transfer-badge--full { color: #7042a6; background: #f4edff; }
.transfer-badge--delta { color: #12744b; background: #e9f8ef; }
.transfer-badge:focus-visible { outline: 2px solid var(--color-primary); outline-offset: 2px; }
.transfer-none { display: inline-flex; align-items: center; gap: 5px; color: var(--color-text-muted); }
.transfer-failed { color: var(--color-danger); }

.conversation-viewport {
  max-height: 620px;
  min-height: 310px;
  padding: 16px 20px 20px;
  overflow-y: auto;
  scroll-behavior: smooth;
}

.message {
  display: grid;
  align-items: flex-start;
  grid-template-columns: 32px minmax(0, 1fr);
  gap: 11px;
}

.message + .message { margin-top: 13px; }

.message-avatar {
  display: grid;
  width: 30px;
  height: 30px;
  margin-top: 7px;
  color: #fff;
  border-radius: 50%;
  place-items: center;
}

.message--a .message-avatar { background: var(--color-cyan); }
.message--b .message-avatar { background: var(--color-orange); }

.message-body {
  padding: 14px 18px 16px;
  border-left: 3px solid;
  border-radius: 5px;
}

.message--a .message-body {
  background: var(--color-cyan-soft);
  border-color: var(--color-cyan);
}

.message--b .message-body {
  background: var(--color-orange-soft);
  border-color: var(--color-orange);
}

.message-body header {
  display: flex;
  min-height: 20px;
  align-items: center;
  gap: 10px;
  color: var(--color-text-muted);
  font-size: 12px;
}

.message-body header strong {
  color: var(--color-text);
  font-size: 13px;
  font-weight: 650;
}

.message-body p {
  margin: 9px 0 0;
  color: #26324c;
  font-size: 14px;
  line-height: 1.75;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.message-tokens { margin-left: auto; }

.message-state {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.message-state--running { color: var(--color-primary); }
.message-state--done { color: var(--color-success); }

.spin { animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

@media (max-width: 680px) {
  .conversation-viewport { padding: 12px; }
  .message { grid-template-columns: 25px minmax(0, 1fr); gap: 7px; }
  .message-avatar { width: 25px; height: 25px; }
  .message-body { padding: 12px; }
  .message-body header { flex-wrap: wrap; gap: 5px 8px; }
  .message-tokens { margin-left: 0; }
  .message-state { margin-left: auto; }
}
</style>
