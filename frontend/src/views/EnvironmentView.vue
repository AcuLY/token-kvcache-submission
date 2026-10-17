<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import {
  Box,
  Check,
  CheckCircle2,
  ChevronRight,
  CircleOff,
  Database,
  FlaskConical,
  Network,
  RefreshCw,
  Save,
  Server,
  ShieldCheck,
} from 'lucide-vue-next'
import { NButton, NIcon, NSelect, useMessage } from 'naive-ui'
import EmptyState from '@/components/EmptyState.vue'
import ModelConnectionPanel from '@/components/ModelConnectionPanel.vue'
import StatusDot from '@/components/StatusDot.vue'
import { useAppStore } from '@/stores/app'
import type { EnvironmentSummary } from '@/types/contracts'
import { cloneData, formatDateTime } from '@/utils/format'

const store = useAppStore()
const message = useMessage()
const draft = ref<EnvironmentSummary | null>(store.environment ? cloneData(store.environment) : null)

watch(
  () => store.environment,
  (value) => {
    draft.value = value ? cloneData(value) : null
  },
  { deep: true },
)

const dirty = computed(() => JSON.stringify(draft.value) !== JSON.stringify(store.environment))
const providers = computed(() => {
  const fromCapabilities = store.capabilities.providers || {}
  if (!draft.value && !Object.keys(fromCapabilities).length) return []
  return [
    {
      kind: 'model',
      index: '1',
      title: 'Model Runtime',
      value: draft.value?.model_provider || fromCapabilities.model,
      icon: Server,
      features: store.capabilities.runtime_features,
      description: '为 Agent 提供 tokenize、prefill、generate 与 seal 等模型运行时合同。',
    },
    {
      kind: 'network',
      index: '2',
      title: 'Network Adapter',
      value: draft.value?.network_provider || fromCapabilities.network,
      icon: Network,
      features: store.capabilities.network_features || [],
      description: '提供方向化网络状态，并由链路适配器将条件施加到真实字节流。',
    },
    {
      kind: 'transfer',
      index: '3',
      title: 'Transfer Handlers',
      value: draft.value?.transfer_provider || fromCapabilities.transfer,
      icon: Database,
      features: store.capabilities.transfer_modes,
      description: '注册可用传输候选；核心状态机只依赖统一处理器合同。',
    },
  ].filter((item) => item.value)
})

const capabilityRows = computed(() => [
  { label: '会话 Token', enabled: store.capabilities.transfer_modes.some((item) => item.toLowerCase().includes('token')) },
  { label: 'KV FULL', enabled: store.supports('kv_full') },
  { label: 'KV DELTA', enabled: store.supports('kv_delta') },
  { label: '自适应', enabled: store.supports('adaptive') },
  { label: '网络可观测', enabled: Boolean(store.capabilities.providers?.network) },
  { label: '适配器工具页', enabled: store.uiExtensions.length > 0 },
])

const environmentList = computed(() =>
  store.environments.length ? store.environments : store.environment ? [store.environment] : [],
)

function restore(): void {
  draft.value = store.environment ? cloneData(store.environment) : null
}

async function save(): Promise<void> {
  if (!draft.value) return
  if (await store.saveEnvironment(draft.value)) message.success('环境配置已保存')
}

async function verify(): Promise<void> {
  await store.loadBootstrap({ preserveDraft: true })
  if (!store.error) message.success('配置与 CapabilityDocument 已重新校验')
}
</script>

<template>
  <div class="environment-layout">
    <aside class="environment-sidebar">
      <h2>环境</h2>
      <button
        v-for="item in environmentList"
        :key="item.id"
        type="button"
        class="environment-row"
        :class="{ 'environment-row--active': item.id === store.environment?.id }"
      >
        <StatusDot :tone="item.id === store.environment?.id ? 'success' : 'muted'" />
        <span class="truncate">{{ item.name }}</span>
        <small v-if="item.id === store.environment?.id">当前</small>
        <ChevronRight :size="16" />
      </button>
      <div v-if="!environmentList.length" class="environment-empty">Coordinator 未返回环境</div>
      <p class="environment-sidebar__note">本轮仅管理 Coordinator 已注册的环境与 Provider。</p>
    </aside>

    <main class="environment-main">
      <ModelConnectionPanel />
      <EmptyState v-if="!draft" title="环境配置不可用" description="请检查 Coordinator 的 /api/v1/environment 响应。" />
      <template v-else>
        <header class="environment-heading">
          <div>
            <div class="environment-title-row">
              <h1>{{ draft.name }}</h1>
              <span class="enabled-tag"><Check :size="14" />已启用</span>
              <span class="valid-tag"><Check :size="14" />配置有效</span>
            </div>
            <p>环境 ID：<span class="mono">{{ draft.environment_id || draft.id }}</span><span>最后更新：{{ formatDateTime(draft.updated_at) }}</span></p>
          </div>
          <div class="environment-actions">
            <NButton @click="verify"><template #icon><NIcon><ShieldCheck /></NIcon></template>验证配置</NButton>
            <NButton type="primary" :disabled="!dirty" :loading="store.actionPending === 'save-environment'" @click="save"><template #icon><NIcon><Save /></NIcon></template>保存</NButton>
            <span v-if="dirty" class="dirty-message"><StatusDot tone="warning" />有未保存的更改</span>
          </div>
        </header>

        <section class="surface adapters-section">
          <h2>适配器</h2>
          <div v-if="providers.length" class="adapter-list">
            <article v-for="provider in providers" :key="provider.kind" class="adapter-row">
              <div class="adapter-identity">
                <span class="adapter-icon"><component :is="provider.icon" :size="22" /></span>
                <div><strong>{{ provider.index }}. {{ provider.title }}</strong><span class="health-tag"><Check :size="12" />健康</span></div>
              </div>
              <div class="adapter-current">
                <small>当前适配器</small>
                <strong class="mono">{{ provider.value }}</strong>
                <p>{{ provider.description }}</p>
              </div>
              <div class="adapter-capabilities">
                <small>能力</small>
                <div><span v-for="feature in provider.features" :key="feature">{{ feature }}</span><em v-if="!provider.features.length">未单独声明</em></div>
              </div>
              <div class="adapter-action">
                <RouterLink v-if="provider.kind === 'network' && store.uiExtensions[0]" :to="store.uiExtensions[0].route" class="tool-link"><FlaskConical :size="17" />打开 {{ store.uiExtensions[0].label }}</RouterLink>
                <NSelect :value="provider.value" :options="[{ label: provider.value, value: provider.value }]" size="small" aria-label="已注册 Provider" />
              </div>
              <CheckCircle2 :size="20" class="adapter-ok" />
            </article>
          </div>
          <EmptyState v-else title="尚无已注册适配器" description="Provider Registry 报告适配器后会在这里展示。" />
        </section>

        <section class="surface derived-section">
          <h2>派生能力</h2>
          <div class="capability-columns">
            <div class="capability-list">
              <span v-for="item in capabilityRows.slice(0, 3)" :key="item.label">
                <component :is="item.enabled ? CheckCircle2 : CircleOff" :class="item.enabled ? 'yes' : 'no'" :size="17" />
                {{ item.label }}
                <strong>{{ item.enabled ? '是' : '否' }}</strong>
              </span>
            </div>
            <div class="capability-list">
              <span v-for="item in capabilityRows.slice(3)" :key="item.label">
                <component :is="item.enabled ? CheckCircle2 : CircleOff" :class="item.enabled ? 'yes' : 'no'" :size="17" />
                {{ item.label }}
                <strong>{{ item.enabled ? '是' : '否' }}</strong>
              </span>
            </div>
            <div class="derived-note"><Box :size="23" /><p>稳定页面由 CapabilityDocument 渲染；适配器工具入口只在扩展声明存在时出现。</p></div>
          </div>
        </section>

        <footer class="environment-health">
          <span><CheckCircle2 :size="20" />配置完整性：<strong>完整</strong></span>
          <span><CheckCircle2 :size="20" />Provider：<strong>{{ providers.length }} 个已注册</strong></span>
          <span><CheckCircle2 :size="20" />未发现加载失败</span>
          <NButton v-if="dirty" text type="primary" @click="restore"><template #icon><NIcon><RefreshCw /></NIcon></template>恢复已保存</NButton>
        </footer>
      </template>
    </main>
  </div>
</template>

<style scoped>
.environment-layout { display: grid; min-height: calc(100vh - var(--header-height)); grid-template-columns: 292px minmax(0, 1fr); }
.environment-sidebar { padding: 25px 20px; background: #fff; border-right: 1px solid var(--color-border); }
.environment-sidebar h2 { margin: 0 4px 14px; font-size: 17px; }
.environment-row { display: grid; width: 100%; min-height: 60px; padding: 0 12px; align-items: center; grid-template-columns: auto minmax(0, 1fr) auto auto; gap: 10px; color: var(--color-text-secondary); cursor: pointer; background: #fff; border: 1px solid var(--color-border); border-radius: 6px; text-align: left; }
.environment-row + .environment-row { margin-top: 9px; }
.environment-row--active { color: var(--color-primary); background: #f8faff; border-color: var(--color-primary); }
.environment-row span { font-size: 14px; font-weight: 600; }
.environment-row small { padding: 3px 6px; color: var(--color-primary); background: var(--color-primary-soft); border-radius: 4px; font-size: 10px; }
.environment-empty { padding: 26px 12px; color: var(--color-text-muted); text-align: center; font-size: 12px; }
.environment-sidebar__note { margin: 18px 4px 0; color: var(--color-text-muted); font-size: 11px; line-height: 1.6; }
.environment-main { width: 100%; max-width: 1380px; padding: 26px 24px 32px; margin: 0 auto; }
.environment-heading { display: flex; min-height: 82px; align-items: flex-start; justify-content: space-between; gap: 20px; }
.environment-title-row { display: flex; align-items: center; gap: 10px; }
.environment-title-row h1 { margin: 0; font-size: 25px; letter-spacing: -.02em; }
.enabled-tag, .valid-tag { display: inline-flex; padding: 4px 8px; align-items: center; gap: 4px; color: var(--color-success); background: var(--color-success-soft); border: 1px solid #cfead7; border-radius: 5px; font-size: 11px; }
.environment-heading p { display: flex; margin: 10px 0 0; gap: 30px; color: var(--color-text-muted); font-size: 11px; }
.environment-actions { display: flex; align-items: center; justify-content: flex-end; gap: 10px; flex-wrap: wrap; }
.dirty-message { display: flex; width: 100%; justify-content: flex-end; align-items: center; gap: 7px; color: var(--color-text-muted); font-size: 10px; }
.adapters-section, .derived-section { padding: 14px 16px 16px; box-shadow: none; }
.adapters-section h2, .derived-section h2 { margin: 0 0 13px; font-size: 16px; }
.adapter-list { display: flex; flex-direction: column; gap: 12px; }
.adapter-row { display: grid; min-height: 134px; padding: 18px 20px; align-items: center; grid-template-columns: 230px minmax(270px, 1.35fr) minmax(220px, 1fr) 210px auto; gap: 18px; border: 1px solid var(--color-border); border-radius: 7px; }
.adapter-identity { display: flex; align-items: center; gap: 15px; }
.adapter-icon { display: grid; width: 52px; height: 52px; color: var(--color-primary); border: 1px solid var(--color-border); border-radius: 7px; place-items: center; }
.adapter-identity > div { display: flex; align-items: flex-start; flex-direction: column; gap: 9px; }
.adapter-identity strong { font-size: 14px; }
.health-tag { display: inline-flex; padding: 3px 7px; align-items: center; gap: 4px; color: var(--color-success); background: var(--color-success-soft); border-radius: 4px; font-size: 10px; }
.adapter-current, .adapter-capabilities { min-width: 0; }
.adapter-current small, .adapter-capabilities small { display: block; margin-bottom: 7px; color: var(--color-text-muted); font-size: 10px; }
.adapter-current strong { color: var(--color-text); font-size: 13px; }
.adapter-current p { margin: 7px 0 0; color: var(--color-text-muted); font-size: 10px; line-height: 1.5; }
.adapter-capabilities > div { display: flex; flex-wrap: wrap; gap: 6px; }
.adapter-capabilities span { padding: 3px 6px; color: var(--color-primary); background: var(--color-primary-soft); border: 1px solid #d6e3ff; border-radius: 4px; font-size: 10px; }
.adapter-capabilities em { color: var(--color-text-muted); font-size: 11px; font-style: normal; }
.adapter-action { display: flex; flex-direction: column; gap: 8px; }
.tool-link { display: flex; min-height: 37px; padding: 0 10px; align-items: center; justify-content: center; gap: 7px; color: var(--color-primary); border: 1px solid var(--color-primary); border-radius: 5px; font-size: 11px; font-weight: 600; }
.adapter-ok { color: var(--color-success); }
.derived-section { margin-top: 14px; }
.capability-columns { display: grid; grid-template-columns: 1fr 1fr 280px; gap: 16px; }
.capability-list { padding: 4px 14px; border: 1px solid var(--color-border-soft); border-radius: 6px; }
.capability-list span { display: grid; min-height: 38px; align-items: center; grid-template-columns: auto 1fr auto; gap: 9px; color: var(--color-text-secondary); border-bottom: 1px solid var(--color-border-soft); font-size: 11px; }
.capability-list span:last-child { border-bottom: 0; }
.capability-list strong { font-weight: 600; }
.yes { color: var(--color-success); }
.no { color: var(--color-danger); }
.derived-note { display: flex; padding: 16px; align-items: center; gap: 12px; color: var(--color-primary); background: #f8faff; border: 1px solid #dce7ff; border-radius: 6px; }
.derived-note p { margin: 0; color: var(--color-text-secondary); font-size: 11px; line-height: 1.65; }
.environment-health { display: flex; min-height: 60px; padding: 12px 18px; margin-top: 14px; align-items: center; justify-content: space-between; gap: 22px; color: var(--color-success); background: #f1faf4; border: 1px solid #cdebd7; border-radius: 7px; }
.environment-health span { display: inline-flex; align-items: center; gap: 7px; color: var(--color-text-secondary); font-size: 11px; }
.environment-health strong { color: var(--color-success); }
@media (max-width: 1500px) {
  .adapter-row { grid-template-columns: 210px 1fr auto; }
  .adapter-capabilities { grid-column: 2; }
  .adapter-action { grid-column: 2; grid-row: 2; }
  .adapter-ok { grid-column: 3; grid-row: 1 / span 2; }
  .capability-columns { grid-template-columns: 1fr 1fr; }
  .derived-note { grid-column: 1 / -1; }
}
@media (max-width: 860px) {
  .environment-layout { grid-template-columns: 1fr; }
  .environment-sidebar { padding: 12px 16px; border-right: 0; border-bottom: 1px solid var(--color-border); }
  .environment-sidebar h2, .environment-sidebar__note { display: none; }
  .environment-row { max-width: 360px; min-height: 46px; }
  .environment-main { padding: 18px 16px 28px; }
  .environment-heading { flex-direction: column; }
  .environment-actions { justify-content: flex-start; margin-bottom: 14px; }
  .dirty-message { width: auto; }
  .adapter-row { grid-template-columns: 1fr auto; }
  .adapter-current, .adapter-capabilities, .adapter-action { grid-column: 1; }
  .adapter-ok { grid-column: 2; grid-row: 1; }
}
@media (max-width: 600px) {
  .environment-main { padding-inline: 12px; }
  .environment-title-row { align-items: flex-start; flex-wrap: wrap; }
  .environment-title-row h1 { width: 100%; font-size: 23px; }
  .environment-heading p { flex-direction: column; gap: 4px; }
  .environment-actions :deep(.n-button) { flex: 1; }
  .adapter-row { padding: 15px; }
  .capability-columns { grid-template-columns: 1fr; }
  .derived-note { grid-column: auto; }
  .environment-health { align-items: flex-start; flex-direction: column; }
}
</style>
