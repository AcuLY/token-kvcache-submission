<script setup lang="ts">
import { computed } from 'vue'
import { ArrowRight, CheckCircle2, Clock3 } from 'lucide-vue-next'
import type { TransferRecord } from '@/types/contracts'
import EmptyState from './EmptyState.vue'
import StatusDot from './StatusDot.vue'
import { formatBytes, formatDuration, formatNumber, modeLabel, shortAgent } from '@/utils/format'

const props = withDefaults(defineProps<{ transfer?: TransferRecord | null; heading?: string }>(), { heading: '当前交接' })

const reason = computed(() => {
  const labels: Record<string, string> = {
    only_supported_mode: '始终使用 Token',
    resident_prefix_delta: '已有有效前缀，只补齐缺失 KV',
    missing_base_full_snapshot: '没有可复用前缀，发送完整 KV',
    kv_lower_marginal_cost: 'KV 的预计交接成本更低',
    token_lower_or_equal_marginal_cost: 'Token 的预计交接成本更低或相等',
    fixed_token_policy: '固定 Token 策略',
    fixed_kv_policy: '固定 KV 策略',
  }
  const raw = props.transfer?.selector_reason || ''
  const suffix = raw.split(':').at(-1) || raw
  return (raw.startsWith('paper_assumptions_unmet') ? '恢复路径采用工程成本：' : '') + (labels[suffix] || suffix || '—')
})

const tone = computed(() => {
  const status = String(props.transfer?.status || '').toLowerCase()
  if (status.match(/complete|success|committed/)) return 'success' as const
  if (status.match(/fail|error/)) return 'danger' as const
  return 'primary' as const
})
</script>

<template>
  <section class="surface handoff-panel" aria-labelledby="handoff-heading">
    <div class="surface-header">
      <h3 id="handoff-heading" class="handoff-heading">{{ heading }}</h3>
      <span v-if="transfer" class="handoff-status"><StatusDot :tone="tone" />{{ transfer.status || '已计划' }}</span>
    </div>
    <EmptyState
      v-if="!transfer"
      compact
      title="暂无交接记录"
      description="首次交接后显示记录；会话结束后仍可回看。"
    />
    <div v-else class="handoff-grid">
      <div class="handoff-direction">
        <span>{{ transfer.source_agent_id ? shortAgent(transfer.source_agent_id) : '—' }}</span>
        <ArrowRight :size="18" />
        <span>{{ transfer.target_agent_id ? shortAgent(transfer.target_agent_id) : '—' }}</span>
      </div>
      <dl>
        <div><dt>阶段</dt><dd>{{ transfer.status || '—' }}</dd></div>
        <div><dt>模式</dt><dd><span class="mode-chip">{{ modeLabel(transfer.mode, transfer.variant) }}</span></dd></div>
        <div><dt>选择原因</dt><dd>{{ reason }}</dd></div>
        <div><dt>本轮输出 α / 标记 / 总上下文</dt><dd class="tabular">{{ formatNumber(transfer.output_token_count) }} / {{ formatNumber(transfer.wrapper_token_count) }} / {{ formatNumber(transfer.token_count) }}</dd></div>
        <div><dt>负载</dt><dd class="tabular">{{ formatBytes(transfer.payload_bytes) }}</dd></div>
        <div><dt>P2P 预测 / 实测</dt><dd class="tabular">{{ formatDuration(transfer.predicted_ms) }} / {{ formatDuration(transfer.actual_ms ?? transfer.timings?.p2p_ms) }}</dd></div>
      </dl>
      <div v-if="transfer.decision" class="decision-summary">
        <strong>{{ transfer.decision.effective_objective === 'paper' ? '论文理论成本比较' : transfer.decision.effective_objective === 'p2p_only' ? 'P2P 成本比较' : '工程交接成本比较' }}</strong>
        <span v-for="candidate in transfer.decision.candidates" :key="candidate.mode">
          {{ candidate.mode }} 预计 {{ formatDuration(candidate.objective_ms) }}
        </span>
        <span>所选路径实测 {{ formatDuration(transfer.timings?.handoff_e2e_ms) }}（完整交接）</span>
      </div>
      <div class="network-strip">
        <span><strong>{{ transfer.network?.bandwidth_mbps ?? '—' }}</strong> Mbps 带宽</span>
        <span><strong>{{ transfer.network?.rtt_ms ?? '—' }}</strong> ms RTT</span>
        <span><strong>{{ transfer.network?.jitter_ms ?? '—' }}</strong> ms 抖动</span>
        <span v-if="tone === 'success'" class="result result--success"><CheckCircle2 :size="15" />已提交</span>
        <span v-else-if="tone === 'danger'" class="result result--failure">交接失败</span>
        <span v-else class="result"><Clock3 :size="15" />处理中</span>
      </div>
    </div>
  </section>
</template>

<style scoped>
.handoff-heading { margin: 0; font-size: 14px; font-weight: 650; }
.handoff-status { display: flex; align-items: center; gap: 7px; color: var(--color-text-secondary); font-size: 12px; }
.handoff-grid { padding: 14px 18px 16px; }
.handoff-direction { display: flex; align-items: center; gap: 9px; color: var(--color-primary); font-size: 19px; font-weight: 700; }
dl { display: grid; margin: 15px 0; grid-template-columns: repeat(3, minmax(0, 1fr)); border: 1px solid var(--color-border-soft); border-radius: 6px; }
dl > div { min-width: 0; padding: 10px 12px; }
dl > div:not(:nth-child(3n + 1)) { border-left: 1px solid var(--color-border-soft); }
dl > div:nth-child(n + 4) { border-top: 1px solid var(--color-border-soft); }
dt { color: var(--color-text-muted); font-size: 11px; }
dd { margin: 4px 0 0; color: var(--color-text); font-size: 12px; overflow-wrap: anywhere; }
.mode-chip { display: inline-flex; padding: 2px 7px; color: #815500; background: #fff0bf; border: 1px solid #f2c14d; border-radius: 3px; font-size: 10px; font-weight: 700; }
.network-strip { display: flex; flex-wrap: wrap; align-items: center; gap: 14px 24px; color: var(--color-text-muted); font-size: 11px; }
.network-strip strong { color: var(--color-text-secondary); font-size: 13px; }
.result { display: inline-flex; margin-left: auto; align-items: center; gap: 5px; color: var(--color-primary); }
.result--success { color: var(--color-success); }
.result--failure { color: var(--color-danger); }
.decision-summary { display: flex; flex-wrap: wrap; gap: 8px 20px; padding: 12px; margin-bottom: 14px; border-radius: 6px; background: var(--color-surface-subtle); color: var(--color-text-secondary); font-size: 12px; }
@media (max-width: 680px) {
  dl { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  dl > div:nth-child(3n + 1) { border-left: initial; }
  dl > div:nth-child(n + 4) { border-top: initial; }
  dl > div:nth-child(even) { border-left: 1px solid var(--color-border-soft); }
  dl > div:nth-child(n + 3) { border-top: 1px solid var(--color-border-soft); }
  .result { margin-left: 0; }
}
</style>
