<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import {
  CheckCircle2,
  CircleAlert,
  Info,
  RefreshCcw,
  Save,
  ShieldCheck,
} from 'lucide-vue-next'
import {
  NButton,
  NIcon,
  NInput,
  NInputNumber,
  NSelect,
  NSwitch,
  useMessage,
} from 'naive-ui'
import AgentRegistrationCard from '@/components/AgentRegistrationCard.vue'
import EmptyState from '@/components/EmptyState.vue'
import ProfileEditor from '@/components/ProfileEditor.vue'
import { useAppStore } from '@/stores/app'

const store = useAppStore()
const router = useRouter()
const message = useMessage()

const firstSpeakerOptions = computed(() => {
  if (!store.config) return []
  return [store.config.agent_a_profile, store.config.agent_b_profile].map((profile) => ({
    label: profile.display_name || profile.agent_id,
    value: profile.agent_id,
  }))
})

const paperFields = [
  { key: 'layers', label: '层数 L' }, { key: 'heads', label: '注意力头数 H' },
  { key: 'head_dim', label: '每头维度' }, { key: 'model_dim', label: '模型维度' },
  { key: 'ff_dim', label: 'FFN 维度' }, { key: 'vocab_size', label: '词表大小' },
  { key: 'receiver_flops', label: '接收端算力（FLOP/s）' },
  { key: 'token_bits', label: 'Token 位宽（bit）' }, { key: 'kv_precision_bits', label: 'KV 精度（bit）' },
  { key: 'compression_ratio', label: '理论压缩倍率 γ' },
] as const

const modeOptions = computed(() => {
  if (!store.config) return []
  const values = [...(store.capabilities.mode_policies || []), store.config.mode_policy]
    .filter((value, index, list) => Boolean(value) && list.indexOf(value) === index)
  return values.map((value) => ({
    label: value === 'always_token' ? '始终 Token' : value === 'always_kv' ? '始终 KV（优先增量）' : value === 'adaptive' ? '自适应 Token / KV' : value,
    value,
  }))
})

const runtimeCompatible = computed(() => {
  if (store.agents.length < 2) return false
  const descriptors = store.agents.slice(0, 2).map((agent) => agent.runtime_descriptor || agent.runtime || {})
  const keys = ['model_id', 'revision', 'tokenizer_id', 'tokenizer_revision', 'protocol_version']
  return keys.every((key) => {
    const values = descriptors.map((descriptor) => descriptor[key]).filter((value) => value !== undefined)
    return values.length < 2 || values.every((value) => value === values[0])
  })
})

const profilesValid = computed(() => {
  if (!store.config) return false
  return [store.config.agent_a_profile, store.config.agent_b_profile].every(
    (profile) => profile.agent_id && profile.display_name.trim() && profile.system_prompt.trim(),
  )
})

const idsUnique = computed(() =>
  Boolean(store.config && store.config.agent_a_profile.agent_id !== store.config.agent_b_profile.agent_id),
)

const strategySupported = computed(() => {
  if (!store.config) return false
  if (store.config.mode_policy !== 'always_token') {
    return (store.capabilities.mode_policies || []).includes(store.config.mode_policy)
  }
  return store.capabilities.transfer_modes.some((mode) => mode.toLowerCase().includes('token'))
})

const formValid = computed(() =>
  Boolean(
    store.config &&
      profilesValid.value &&
      idsUnique.value &&
      strategySupported.value &&
      store.config.initial_input.trim() &&
      store.config.max_agent_turns > 0 &&
      store.config.max_context_tokens > 0,
  ),
)

const validations = computed(() => [
  { label: 'Agent ID 不同', detail: store.config ? `${store.config.agent_a_profile.agent_id} / ${store.config.agent_b_profile.agent_id}` : '无配置', ok: idsUnique.value },
  { label: '两个 Agent READY', detail: `${store.registeredReady}/2 已就绪`, ok: store.registeredReady >= 2 },
  { label: '运行时描述兼容', detail: runtimeCompatible.value ? '关键运行时字段一致' : '等待兼容的注册信息', ok: runtimeCompatible.value },
  { label: 'Profiles 有效', detail: '名称和系统提示完整', ok: profilesValid.value },
  { label: '所选策略受支持', detail: store.config?.mode_policy || '无配置', ok: strategySupported.value },
])

async function save(returnToConversation = false): Promise<void> {
  if (!formValid.value) return
  if (await store.saveConfig()) {
    message.success('配置已保存，新会话将使用该版本')
    if (returnToConversation) await router.push('/sessions')
  }
}
</script>

<template>
  <div class="page-shell config-page">
    <div class="page-heading">
      <div>
        <h1>Agent 与配置</h1>
        <p>新会话直接使用这里保存的当前配置；修改不会影响正在运行的会话。</p>
      </div>
      <div class="heading-actions">
        <span class="save-state" :class="{ 'save-state--dirty': store.configDirty }">
          <component :is="store.configDirty ? CircleAlert : CheckCircle2" :size="16" />
          {{ store.configDirty ? '有未保存更改' : '所有更改已保存' }}
        </span>
        <NButton :disabled="!store.configDirty" @click="store.restoreConfig()">
          <template #icon><NIcon><RefreshCcw /></NIcon></template>
          恢复未保存更改
        </NButton>
        <NButton type="primary" :disabled="!store.configDirty || !formValid" :loading="store.actionPending === 'save-config'" @click="save(false)">
          <template #icon><NIcon><Save /></NIcon></template>
          保存配置
        </NButton>
      </div>
    </div>

    <template v-if="store.config">
      <section class="surface configuration-section">
        <div class="section-title-row">
          <h2>提示词</h2>
          <p>公共任务告诉双方讨论什么，各自的提示词说明如何参与。</p>
        </div>
          <label class="initial-input">
            <span>公共任务</span>
            <NInput v-model:value="store.config.initial_input" type="textarea" :autosize="{ minRows: 3, maxRows: 8 }" placeholder="双方共同完成的任务或讨论的话题。" />
          </label>
        <div class="profile-editors">
          <ProfileEditor v-model="store.config.agent_a_profile" label="Agent A" accent="a" />
          <ProfileEditor v-model="store.config.agent_b_profile" label="Agent B" accent="b" />
        </div>
      </section>

      <section class="surface configuration-section">
        <div class="section-title-row">
          <h2>会话设置</h2>
          <p>作为新会话的起始配置快照。</p>
        </div>
        <div class="defaults-grid">
          <label>
            <span>第一位发言者</span>
            <NSelect v-model:value="store.config.first_speaker" :options="firstSpeakerOptions" />
          </label>
          <label>
            <span>最大 Agent 发言次数</span>
            <NInputNumber v-model:value="store.config.max_agent_turns" :min="1" :precision="0">
              <template #suffix>次</template>
            </NInputNumber>
          </label>
          <label>
            <span>最大上下文 Tokens</span>
            <NInputNumber v-model:value="store.config.max_context_tokens" :min="1" :precision="0">
              <template #suffix>tokens</template>
            </NInputNumber>
          </label>
          <label class="switch-field">
            <span>遇到 EOS 时停止</span>
            <span><NSwitch v-model:value="store.config.stop_on_eos" />{{ store.config.stop_on_eos ? '是' : '否' }}</span>
          </label>
          <label>
            <span>传输策略</span>
            <NSelect v-model:value="store.config.mode_policy" :options="modeOptions" />
          </label>
        </div>
        <div v-if="store.config.decision && store.capabilities.mode_policies?.includes('adaptive')" class="decision-settings">
          <label><span>决策目标</span><NSelect v-model:value="store.config.decision.objective" :options="[{ label: '论文边际成本（理论模型）', value: 'paper' }, { label: '完整交接耗时（含模型服务）', value: 'mode_e2e' }, { label: '仅 P2P 传输耗时', value: 'p2p_only' }]" /></label>
          <label><span>共享历史窗口（Tokens）</span><NInputNumber v-model:value="store.config.decision.history_window_tokens" :min="64" :precision="0" placeholder="空值表示不截断" /></label>
          <p>两端共用公共任务和双方提示词。正常交接只传新增内容；窗口超限保留公共提示词，重建较近历史并换 epoch。</p>
          <details><summary>论文模型参数（仅用于理论成本）</summary>
            <div class="decision-overheads">
              <label v-for="field in paperFields" :key="field.key"><span>{{ field.label }}</span><NInputNumber v-model:value="store.config.decision.paper[field.key]" :min="1" /></label>
            </div>
            <p>按 Eq. (1)、(18)、(19) 计算；理论 KV 大小与 Mock 协议字节数分列。缓存缺口或恢复不满足正常路径假设时，明确使用工程比较。</p>
          </details>
          <details><summary>额外开销估计（ms，可按实测校准）</summary>
            <div class="decision-overheads">
              <label><span>Token 准备</span><NInputNumber v-model:value="store.config.decision.token_prepare_ms" :min="0" /></label>
              <label><span>Token 恢复链路</span><NInputNumber v-model:value="store.config.decision.token_restore_overhead_ms" :min="0" /></label>
              <label><span>KV 导出与下载</span><NInputNumber v-model:value="store.config.decision.kv_export_overhead_ms" :min="0" /></label>
              <label><span>KV 上传与导入</span><NInputNumber v-model:value="store.config.decision.kv_import_overhead_ms" :min="0" /></label>
            </div>
            <p>这些是 HTTP、序列化等额外开销的估计值；模型延迟另由 Mock 实验室配置并实际执行。</p>
          </details>
        </div>
        <div class="capability-note"><Info :size="15" />当前环境能力：{{ store.capabilities.transfer_modes.join('、') || '未上报传输模式' }}。不可用策略不会被静默保存。</div>
      </section>
    </template>

    <section v-else class="surface">
      <EmptyState title="当前配置不可用" description="Coordinator 未返回 SessionConfig，请重新加载或检查服务日志。" />
    </section>

    <details class="surface configuration-section registration-details">
      <summary>运行状态 · {{ store.registeredReady }}/2 Agent 就绪</summary>
      <div v-if="store.agents.length" class="registration-grid">
        <AgentRegistrationCard
          v-for="(agent, index) in store.agents.slice(0, 2)"
          :key="agent.agent_id"
          :agent="agent"
          :accent="index === 0 ? 'a' : 'b'"
        />
      </div>
      <EmptyState v-else compact title="等待 Agent 注册" description="启动两个 Agent 后，实例 ID、端点和运行时能力会自动出现在这里。" />
      <div class="section-note"><Info :size="15" />心跳超过 Coordinator 阈值时，Agent 将显示为离线；注册字段不能在此编辑。</div>
    </details>

    <footer class="validation-footer">
      <div class="validation-panel surface">
        <div class="validation-title"><ShieldCheck :size="19" />配置校验结果</div>
        <div v-for="item in validations" :key="item.label" class="validation-item" :class="{ 'validation-item--ok': item.ok }">
          <component :is="item.ok ? CheckCircle2 : CircleAlert" :size="16" />
          <span><strong>{{ item.label }}</strong><small>{{ item.detail }}</small></span>
        </div>
      </div>
      <NButton :disabled="!store.configDirty" @click="store.restoreConfig()">恢复未保存更改</NButton>
      <NButton type="primary" :disabled="!store.configDirty || !formValid" :loading="store.actionPending === 'save-config'" @click="save(true)">
        <template #icon><NIcon><Save /></NIcon></template>
        保存并返回会话
      </NButton>
    </footer>
  </div>
</template>

<style scoped>
.decision-settings { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 12px; margin-top: 16px; }
.decision-settings label { display: grid; gap: 6px; font-size: 12px; }
.decision-settings p, .decision-settings details { grid-column: 1 / -1; margin: 0; font-size: 12px; color: var(--color-text-secondary); }
.decision-overheads { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin: 12px 0; }
.registration-details summary { padding-block: 16px; cursor: pointer; font-size: 13px; }
.config-page { --section-padding: 16px; }
.heading-actions { display: flex; align-items: center; justify-content: flex-end; gap: 10px; }
.save-state { display: inline-flex; align-items: center; gap: 6px; color: var(--color-success); font-size: 12px; white-space: nowrap; }
.save-state--dirty { color: var(--color-warning); }
.configuration-section { margin-bottom: 10px; padding: 0 var(--section-padding) 14px; }
.section-title-row { display: flex; min-height: 52px; align-items: center; gap: 20px; }
.section-title-row h2 { margin: 0; font-size: 15px; font-weight: 700; }
.section-title-row p { margin: 0; color: var(--color-text-muted); font-size: 11px; }
.registration-grid, .profile-editors { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.profile-editors { gap: 0; }
.initial-input { display: grid; gap: 8px; margin: 0 16px 20px; font-size: 12px; color: var(--color-text-secondary); }
.section-note, .capability-note { display: flex; margin-top: 9px; align-items: center; gap: 7px; color: var(--color-text-muted); font-size: 11px; }
.section-note svg, .capability-note svg { color: var(--color-primary); }
.defaults-grid { display: grid; align-items: start; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 14px; }
.defaults-grid label { display: flex; min-width: 0; flex-direction: column; gap: 6px; }
.defaults-grid label > span:first-child { color: var(--color-text-secondary); font-size: 11px; font-weight: 600; }
.switch-field > span:last-child { display: flex; min-height: 40px; align-items: center; gap: 9px; color: var(--color-text-secondary); font-size: 12px; }
:deep(.n-input-number) { width: 100%; }
.capability-note { padding: 8px 10px; background: var(--color-primary-soft); border-radius: 5px; }
.validation-footer { display: grid; margin-top: 4px; align-items: center; grid-template-columns: minmax(0, 1fr) auto auto; gap: 12px; }
.validation-panel { display: flex; min-height: 72px; padding: 12px 16px; align-items: center; justify-content: space-between; gap: 16px; }
.validation-title { display: flex; align-items: center; gap: 8px; font-size: 13px; font-weight: 700; }
.validation-item { display: flex; min-width: 0; align-items: center; gap: 7px; color: var(--color-danger); }
.validation-item--ok { color: var(--color-success); }
.validation-item > span { display: flex; min-width: 0; flex-direction: column; }
.validation-item strong { color: var(--color-text-secondary); font-size: 11px; }
.validation-item small { color: var(--color-text-muted); font-size: 9px; }
@media (max-width: 1320px) {
  .defaults-grid { grid-template-columns: repeat(3, 1fr); }
  .initial-input { grid-column: 1 / -1; }
  .validation-panel { display: grid; grid-template-columns: repeat(3, 1fr); }
  .validation-title { grid-column: 1 / -1; }
}
@media (max-width: 950px) {
  .heading-actions { flex-wrap: wrap; justify-content: flex-start; }
  .registration-grid, .profile-editors { grid-template-columns: 1fr; }
  .defaults-grid { grid-template-columns: 1fr 1fr; }
  .validation-footer { grid-template-columns: 1fr 1fr; }
  .validation-panel { grid-column: 1 / -1; }
}
@media (max-width: 620px) {
  .heading-actions :deep(.n-button) { flex: 1; }
  .save-state { width: 100%; }
  .section-title-row { align-items: flex-start; flex-direction: column; gap: 4px; padding-block: 13px; }
  .defaults-grid { grid-template-columns: 1fr; }
  .initial-input { grid-column: auto; }
  .validation-panel { grid-template-columns: 1fr; }
  .validation-title { grid-column: auto; }
  .validation-footer { grid-template-columns: 1fr; }
}
</style>
