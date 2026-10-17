<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  CheckCircle2,
  CircleOff,
  Download,
  FileClock,
  Info,
  Network,
} from 'lucide-vue-next'
import { NButton, NIcon, NSelect, NTabPane, NTabs } from 'naive-ui'
import CurrentHandoffPanel from '@/components/CurrentHandoffPanel.vue'
import EmptyState from '@/components/EmptyState.vue'
import NetworkChart from '@/components/NetworkChart.vue'
import PhaseTimingChart from '@/components/PhaseTimingChart.vue'
import StatusDot from '@/components/StatusDot.vue'
import TransferDurationChart from '@/components/TransferDurationChart.vue'
import { useAppStore } from '@/stores/app'
import type { ObservationEvent, TransferRecord } from '@/types/contracts'
import {
  directionLabel,
  eventTypeLabel,
  formatBytes,
  formatDateTime,
  formatDuration,
  formatNumber,
  modeLabel,
} from '@/utils/format'

const store = useAppStore()
const tab = ref('overview')
const selectedSessionId = ref(store.selectedSession?.id || '')
const direction = ref('all')

watch(
  () => store.selectedSession?.id,
  (value) => {
    if (!selectedSessionId.value && value) selectedSessionId.value = value
  },
)

const sessionOptions = computed(() =>
  store.sessions.map((session) => ({
    label: session.title || session.config?.initial_input || session.id,
    value: session.id,
  })),
)

const selectedSession = computed(() =>
  store.sessions.find((session) => session.id === selectedSessionId.value) ||
  (store.activeSession?.id === selectedSessionId.value ? store.activeSession : null),
)

const transfers = computed(() => selectedSession.value?.transfers || selectedSession.value?.handoffs || [])
const directionOptions = computed(() => {
  const config = selectedSession.value?.config
  if (!config) return [{ label: '全部', value: 'all' }]
  const a = config.agent_a_profile
  const b = config.agent_b_profile
  return [
    { label: '全部', value: 'all' },
    { label: `${a.display_name || a.agent_id} → ${b.display_name || b.agent_id}`, value: `${a.agent_id}->${b.agent_id}`.toLowerCase() },
    { label: `${b.display_name || b.agent_id} → ${a.display_name || a.agent_id}`, value: `${b.agent_id}->${a.agent_id}`.toLowerCase() },
  ]
})
const filteredTransfers = computed(() => {
  if (direction.value === 'all') return transfers.value
  return transfers.value.filter((item) => {
    const pair = `${item.source_agent_id || ''}->${item.target_agent_id || ''}`.toLowerCase()
    return pair === direction.value
  })
})

const sessionEvents = computed(() => {
  if (!selectedSession.value) return []
  return store.events.filter(
    (event) => (event.conversation_id || event.session_id) === selectedSession.value?.id,
  )
})

const cacheSupported = computed(() =>
  ['kv', 'kv_full', 'kv_delta', 'cache_metrics'].some((feature) => store.supports(feature)),
)
const realModelConnected = computed(() => store.capabilities.providers?.model === 'huggingface')

const totals = computed(() => {
  const items = filteredTransfers.value
  const tokenValues = items.map((item) => item.token_count).filter((value): value is number => typeof value === 'number')
  const byteValues = items.map((item) => item.payload_bytes).filter((value): value is number => typeof value === 'number')
  const durationValues = items
    .map((item) => item.actual_ms ?? item.timings?.total_ms)
    .filter((value): value is number => typeof value === 'number')
  return {
    count: items.length,
    tokens: tokenValues.length ? tokenValues.reduce((sum, value) => sum + value, 0) : undefined,
    bytes: byteValues.length ? byteValues.reduce((sum, value) => sum + value, 0) : undefined,
    average: durationValues.length
      ? durationValues.reduce((sum, value) => sum + value, 0) / durationValues.length
      : undefined,
  }
})

const latestTransfer = computed(() => filteredTransfers.value.at(-1))

function eventSummary(event: ObservationEvent): string {
  const payload = event.payload || {}
  const value = payload.message || payload.message_text || payload.reason || payload.status || payload.phase
  return typeof value === 'string' ? value : '—'
}

function statusTone(status?: string): 'success' | 'primary' | 'warning' | 'danger' | 'muted' {
  const value = String(status || '').toLowerCase()
  if (value.match(/commit|complete|success/)) return 'success'
  if (value.match(/fail|error/)) return 'danger'
  if (value.match(/plan|execut|prepar/)) return 'primary'
  return 'muted'
}

function keyFor(record: TransferRecord, index: number): string {
  return record.transfer_id || `transfer-${index}`
}
</script>

<template>
  <div class="page-shell experiment-page">
    <h1 class="sr-only">实验分析</h1>
    <NTabs v-model:value="tab" type="line" size="large" class="analysis-tabs" animated>
      <NTabPane name="overview" tab="概览">
        <div class="filter-bar surface">
          <label><span>会话</span><NSelect v-model:value="selectedSessionId" :options="sessionOptions" placeholder="选择会话" /></label>
          <label><span>方向</span><NSelect v-model:value="direction" :options="directionOptions" /></label>
          <label class="target-field"><span>目标（模式）</span><strong>{{ selectedSession?.config?.decision?.objective === 'paper' ? '论文边际成本' : selectedSession?.config?.decision?.objective === 'p2p_only' ? '仅 P2P' : '完整交接耗时' }}</strong></label>
          <NButton :disabled="!selectedSession" :loading="store.actionPending === 'export-csv'" @click="store.exportCsv(selectedSession?.id)">
            <template #icon><NIcon><Download /></NIcon></template>
            导出 CSV
          </NButton>
        </div>

        <EmptyState v-if="!selectedSession" title="暂无可分析会话" description="会话完成第一条观测后，Token 交接与耗时数据会出现在这里。" />
        <template v-else>
          <div class="metric-strip surface">
            <div><small>状态交接</small><strong>{{ totals.count }}</strong><span>次</span></div>
            <div><small>累计 Tokens</small><strong>{{ formatNumber(totals.tokens) }}</strong></div>
            <div><small>累计负载</small><strong>{{ formatBytes(totals.bytes) }}</strong></div>
            <div><small>平均实测耗时</small><strong>{{ formatDuration(totals.average) }}</strong></div>
          </div>

          <div class="overview-grid">
            <section class="surface chart-panel">
              <div class="surface-header"><h2 class="surface-title">交接模式与耗时</h2><span class="muted">按发言顺序</span></div>
              <div v-if="filteredTransfers.length" class="transfer-rail">
                <span v-for="(item, index) in filteredTransfers" :key="keyFor(item, index)">
                  <small>T{{ item.turn_index ?? item.turn_id ?? index + 1 }}</small>
                  <em>{{ modeLabel(item.mode, item.variant) }}</em>
                </span>
              </div>
              <TransferDurationChart :transfers="filteredTransfers" :height="285" />
            </section>
            <CurrentHandoffPanel :transfer="latestTransfer" />
          </div>

          <section class="surface chart-panel phase-chart">
            <div class="surface-header"><h2 class="surface-title">阶段耗时</h2><span class="muted">毫秒</span></div>
            <PhaseTimingChart :transfers="filteredTransfers" :height="255" />
          </section>

          <section class="future-note surface">
            <Info :size="25" />
            <div>
              <h2>能力与后续分析</h2>
              <p v-if="realModelConnected">当前连接 Hugging Face 真实模型，展示 Token 与真实 KV Tensor 的交接记录。预测成本和各阶段实测耗时分别展示；网络条件以每次交接绑定的快照为准。</p>
              <p v-else>当前环境提供 Token 与 Mock KV 的实测传输分析。KV 数据为确定性模拟缓存；自适应按本次交接成本选择。</p>
            </div>
          </section>
        </template>
      </NTabPane>

      <NTabPane name="network" tab="网络">
        <div class="tab-stack">
          <div class="filter-bar surface">
            <label><span>会话</span><NSelect v-model:value="selectedSessionId" :options="sessionOptions" placeholder="选择会话" /></label>
            <label><span>方向</span><NSelect v-model:value="direction" :options="directionOptions" /></label>
          </div>
          <section class="surface chart-panel">
            <div class="surface-header"><h2 class="surface-title">版本化网络快照</h2><span class="network-label"><Network :size="15" />带宽 / RTT</span></div>
            <NetworkChart :transfers="filteredTransfers" :events="sessionEvents" :height="430" />
          </section>
        </div>
      </NTabPane>

      <NTabPane name="cache" tab="Cache">
        <section class="surface capability-empty">
          <div v-if="filteredTransfers.some((item) => item.mode === 'KV')" class="responsive-table">
            <h2 class="surface-title">{{ realModelConnected ? '真实 KV 传输' : 'Mock KV 传输' }}</h2>
            <p>复用连续缓存前缀，传输缺失区间的 KV 与 Token sidecar。ξ 表示接收端缺失的已共享历史。</p>
            <table>
              <thead><tr><th>Turn</th><th>模式</th><th>复用前缀</th><th>缺失历史 ξ</th><th>传输区间</th><th>负载</th><th>状态</th></tr></thead>
              <tbody><tr v-for="item in filteredTransfers.filter((row) => row.mode === 'KV')" :key="item.transfer_id">
                <td>{{ item.turn_id }}</td><td>KV / {{ item.variant }}</td>
                <td>{{ item.start_token }}</td><td>{{ item.xi_tokens }}</td>
                <td>[{{ item.start_token }}, {{ item.token_count }})</td>
                <td>{{ item.payload_bytes }} B</td><td>{{ item.status }}</td>
              </tr></tbody>
            </table>
          </div>
          <EmptyState
            v-else
            :kind="cacheSupported ? 'empty' : 'unavailable'"
            :title="cacheSupported ? '暂无 Cache 观测数据' : '当前环境未提供 Cache / KV 能力'"
            :description="cacheSupported ? '在配置页选择始终 KV 并运行会话后，可查看 Cache 传输记录。' : '当前服务未声明 KV 能力。'"
          />
          <div class="capability-facts">
            <span><component :is="cacheSupported ? CheckCircle2 : CircleOff" :size="17" />KV FULL：{{ store.supports('kv_full') ? '可用' : '不可用' }}</span>
            <span><component :is="store.supports('kv_delta') ? CheckCircle2 : CircleOff" :size="17" />KV DELTA：{{ store.supports('kv_delta') ? '可用' : '不可用' }}</span>
            <span><component :is="store.supports('adaptive') ? CheckCircle2 : CircleOff" :size="17" />Adaptive：{{ store.supports('adaptive') ? '可用' : '不可用' }}</span>
          </div>
        </section>
      </NTabPane>

      <NTabPane name="transfers" tab="交接明细">
        <div class="tab-stack">
          <div class="filter-bar surface">
            <label><span>会话</span><NSelect v-model:value="selectedSessionId" :options="sessionOptions" placeholder="选择会话" /></label>
            <label><span>方向</span><NSelect v-model:value="direction" :options="directionOptions" /></label>
            <NButton :disabled="!selectedSession" @click="store.exportCsv(selectedSession?.id)"><template #icon><NIcon><Download /></NIcon></template>导出 CSV</NButton>
          </div>
          <section class="surface table-surface">
            <EmptyState v-if="!filteredTransfers.length" title="暂无交接明细" description="Token 交接完成后显示计划、负载与实测耗时。" />
            <div v-else class="responsive-table">
              <table>
                <thead><tr><th>Turn</th><th>方向</th><th>模式</th><th>Tokens</th><th>负载</th><th>预测耗时</th><th>实测耗时</th><th>状态</th></tr></thead>
                <tbody>
                  <tr v-for="(item, index) in filteredTransfers" :key="keyFor(item, index)">
                    <td>{{ item.turn_index ?? item.turn_id ?? index + 1 }}</td>
                    <td>{{ directionLabel(item.source_agent_id, item.target_agent_id, item.direction) }}</td>
                    <td><span class="mode-tag">{{ modeLabel(item.mode, item.variant) }}</span></td>
                    <td>{{ formatNumber(item.token_count) }}</td>
                    <td>{{ formatBytes(item.payload_bytes) }}</td>
                    <td>{{ formatDuration(item.predicted_ms) }}</td>
                    <td>{{ formatDuration(item.actual_ms ?? item.timings?.total_ms) }}</td>
                    <td><span class="table-status"><StatusDot :tone="statusTone(item.status)" />{{ item.status || '—' }}</span></td>
                  </tr>
                </tbody>
              </table>
            </div>
          </section>
        </div>
      </NTabPane>

      <NTabPane name="decision" tab="自适应决策">
        <div class="tab-stack">
          <div class="filter-bar surface">
            <label><span>会话</span><NSelect v-model:value="selectedSessionId" :options="sessionOptions" placeholder="选择会话" /></label>
            <NButton :disabled="!selectedSession" @click="store.exportCsv(selectedSession?.id)">导出 CSV</NButton>
          </div>
          <section class="surface table-surface">
            <div class="surface-header"><h2 class="surface-title">Token / KV 候选成本</h2></div>
            <p class="decision-note">参照论文 §5.2：Token − KV &gt; 0 时选择 KV。比较目标：{{ selectedSession?.config?.decision?.objective === 'paper' ? '论文边际成本' : selectedSession?.config?.decision?.objective === 'p2p_only' ? '仅 P2P' : '完整交接耗时' }}。本地 Prefill、窗口重建与生成是共同阶段，不参与选择。</p>
            <EmptyState v-if="!filteredTransfers.some((item) => item.decision)" title="暂无自适应决策" description="选择自适应策略并运行会话后，可查看两种候选成本。固定策略会话仍保留实测交接数据。" />
            <div v-else class="responsive-table">
              <table><thead><tr><th>Turn</th><th>候选</th><th>协议负载 / 帧</th><th>论文理论量 / 耗时</th><th>准备 / 导出</th><th>P2P</th><th>恢复 / 导入</th><th>完整交接</th><th>比较值</th><th>选择 / 实测交接</th><th>α / 标记 / θ / ξ</th><th>epoch / 截断 / 比较口径</th></tr></thead>
                <tbody><template v-for="item in filteredTransfers.filter((row) => row.decision)" :key="item.transfer_id">
                  <tr v-for="cost in item.decision!.candidates" :key="cost.mode">
                    <td>{{ item.turn_id }}</td><td>{{ cost.mode }} / {{ cost.scope }}</td>
                    <td>{{ cost.payload_bytes }} / {{ cost.frame_bytes }} B</td>
                    <td>{{ formatBytes(cost.paper_bits / 8) }} / {{ formatDuration(cost.paper_ms) }}</td>
                    <td>{{ formatDuration(cost.sender_ms) }}</td><td>{{ formatDuration(cost.p2p_ms) }}</td>
                    <td>{{ formatDuration(cost.receiver_ms) }}</td><td>{{ formatDuration(cost.mode_e2e_ms) }}</td>
                    <td>{{ formatDuration(cost.objective_ms) }}</td>
                    <td>{{ item.mode === cost.mode ? '已选择' : '未选择' }} / {{ item.mode === cost.mode ? formatDuration(item.timings?.handoff_e2e_ms) : '—' }}</td>
                    <td>{{ item.decision!.current_tokens }} / {{ item.decision!.wrapper_tokens }} / {{ item.decision!.theta_tokens }} / {{ item.decision!.xi_tokens }}</td>
                    <td>{{ item.context_epoch }} / {{ item.trimmed_tokens }} / {{ item.decision!.effective_objective }}</td>
                  </tr>
                </template></tbody>
              </table>
            </div>
            <p class="decision-note">论文量按模型维度、精度与 γ 计算；工程预测包含协议帧、模型调用和恢复开销。未选模式没有实测值；缺口/恢复路径会标明工程比较口径。</p>
          </section>
        </div>
      </NTabPane>

      <NTabPane name="events" tab="事件">
        <div class="tab-stack">
          <div class="filter-bar surface">
            <label><span>会话</span><NSelect v-model:value="selectedSessionId" :options="sessionOptions" placeholder="选择会话" /></label>
            <span class="seq-summary"><FileClock :size="16" />最新 seq <strong class="mono">{{ store.latestSeq }}</strong></span>
          </div>
          <section class="surface table-surface">
            <EmptyState v-if="!sessionEvents.length" title="暂无事件" description="WebSocket 事件会按全局单调 seq 记录。" />
            <div v-else class="responsive-table">
              <table>
                <thead><tr><th>Seq</th><th>时间</th><th>类型</th><th>Agent</th><th>Turn</th><th>Transfer</th><th>摘要</th></tr></thead>
                <tbody>
                  <tr v-for="event in [...sessionEvents].reverse()" :key="event.seq">
                    <td class="mono">#{{ event.seq }}</td>
                    <td>{{ formatDateTime(event.timestamp) }}</td>
                    <td>{{ eventTypeLabel(event.type) }}</td>
                    <td>{{ event.agent_id || '—' }}</td>
                    <td>{{ event.turn_id ?? '—' }}</td>
                    <td class="mono">{{ event.transfer_id || '—' }}</td>
                    <td class="event-summary">{{ eventSummary(event) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </section>
        </div>
      </NTabPane>
    </NTabs>
  </div>
</template>

<style scoped>
.decision-note { margin: 12px 18px; color: var(--color-text-secondary); font-size: 12px; line-height: 1.6; }
.experiment-page { padding-top: 8px; }
.analysis-tabs :deep(.n-tabs-nav) { border-bottom: 1px solid var(--color-border); }
.analysis-tabs :deep(.n-tabs-tab) { min-width: 92px; justify-content: center; }
.analysis-tabs :deep(.n-tabs-pane-wrapper) { padding-top: 16px; }
.filter-bar { display: flex; min-height: 68px; padding: 12px 18px; align-items: end; gap: 28px; box-shadow: none; }
.filter-bar label { display: grid; min-width: 210px; grid-template-columns: auto minmax(150px, 1fr); align-items: center; gap: 10px; }
.filter-bar label > span { color: var(--color-text-secondary); font-size: 12px; font-weight: 600; white-space: nowrap; }
.filter-bar .target-field { margin-left: auto; }
.target-field strong { display: flex; min-height: 40px; padding: 0 12px; align-items: center; color: var(--color-text-secondary); border: 1px solid var(--color-border); border-radius: 5px; font-size: 12px; font-weight: 500; }
.metric-strip { display: grid; margin-top: 12px; grid-template-columns: repeat(4, 1fr); box-shadow: none; }
.metric-strip > div { display: flex; min-height: 84px; padding: 14px 20px; align-items: baseline; gap: 7px; flex-wrap: wrap; }
.metric-strip > div + div { border-left: 1px solid var(--color-border-soft); }
.metric-strip small { width: 100%; color: var(--color-text-muted); font-size: 11px; }
.metric-strip strong { color: var(--color-text); font-size: 22px; font-weight: 700; font-variant-numeric: tabular-nums; }
.metric-strip span { color: var(--color-text-muted); font-size: 11px; }
.overview-grid { display: grid; margin-top: 12px; align-items: stretch; grid-template-columns: minmax(0, 2fr) minmax(300px, 0.8fr); gap: 12px; }
.chart-panel { overflow: hidden; box-shadow: none; }
.chart-panel .muted, .surface-header .muted { font-size: 11px; }
.transfer-rail { display: flex; padding: 12px 38px 0; gap: 4px; overflow-x: auto; }
.transfer-rail > span { display: flex; min-width: 56px; flex: 1; align-items: center; flex-direction: column; gap: 3px; }
.transfer-rail small { color: var(--color-text-secondary); font-size: 10px; }
.transfer-rail em { padding: 2px 4px; color: #805400; background: #fff0bc; border: 1px solid #efc059; border-radius: 3px; font-size: 8px; font-style: normal; font-weight: 700; white-space: nowrap; }
.phase-chart { margin-top: 12px; }
.future-note { display: flex; min-height: 78px; padding: 16px 20px; margin-top: 12px; align-items: center; gap: 14px; color: var(--color-primary); box-shadow: none; }
.future-note h2 { margin: 0; color: var(--color-text); font-size: 14px; }
.future-note p { margin: 4px 0 0; color: var(--color-text-secondary); font-size: 12px; line-height: 1.5; }
.tab-stack { display: flex; flex-direction: column; gap: 12px; }
.network-label, .seq-summary { display: inline-flex; align-items: center; gap: 7px; color: var(--color-text-muted); font-size: 11px; }
.seq-summary { align-self: center; margin-left: auto; font-size: 12px; }
.capability-empty { overflow: hidden; box-shadow: none; }
.capability-facts { display: flex; min-height: 62px; align-items: center; justify-content: center; gap: 50px; background: var(--color-surface-subtle); border-top: 1px solid var(--color-border-soft); }
.capability-facts span { display: inline-flex; align-items: center; gap: 7px; color: var(--color-text-secondary); font-size: 12px; }
.table-surface { overflow: hidden; box-shadow: none; }
.responsive-table { width: 100%; overflow-x: auto; }
table { width: 100%; border-collapse: collapse; color: var(--color-text-secondary); font-size: 12px; white-space: nowrap; }
th { height: 44px; padding: 0 14px; color: var(--color-text-muted); background: var(--color-surface-subtle); border-bottom: 1px solid var(--color-border); font-size: 11px; font-weight: 600; text-align: left; }
td { height: 48px; padding: 0 14px; border-bottom: 1px solid var(--color-border-soft); }
tbody tr:last-child td { border-bottom: 0; }
tbody tr:hover { background: #f9fbff; }
.mode-tag { padding: 3px 6px; color: #805400; background: #fff0bc; border: 1px solid #efc059; border-radius: 3px; font-size: 9px; font-weight: 700; }
.table-status { display: inline-flex; align-items: center; gap: 6px; }
.event-summary { max-width: 340px; overflow: hidden; text-overflow: ellipsis; }
@media (max-width: 1080px) {
  .overview-grid { grid-template-columns: 1fr; }
  .filter-bar { flex-wrap: wrap; gap: 12px; }
  .filter-bar .target-field { margin-left: 0; }
  .filter-bar > .n-button { margin-left: auto; }
}
@media (max-width: 720px) {
  .analysis-tabs :deep(.n-tabs-nav-scroll-content) { min-width: 520px; }
  .filter-bar { align-items: stretch; flex-direction: column; }
  .filter-bar label { width: 100%; grid-template-columns: 64px minmax(0, 1fr); }
  .filter-bar > .n-button { width: 100%; margin-left: 0; }
  .metric-strip { grid-template-columns: 1fr 1fr; }
  .metric-strip > div:nth-child(3) { border-left: 0; border-top: 1px solid var(--color-border-soft); }
  .metric-strip > div:nth-child(4) { border-top: 1px solid var(--color-border-soft); }
  .capability-facts { padding: 16px; align-items: flex-start; flex-direction: column; gap: 12px; }
  .future-note { align-items: flex-start; }
}
</style>
