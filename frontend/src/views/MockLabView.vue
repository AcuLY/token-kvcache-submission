<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import {
  CheckCircle2,
  ChevronRight,
  Copy,
  Eye,
  FlaskConical,
  Hash,
  Info,
  RefreshCcw,
  Save,
  Target,
} from 'lucide-vue-next'
import { NButton, NIcon, NInput, NInputNumber, NTabPane, NTabs, useMessage } from 'naive-ui'
import EmptyState from '@/components/EmptyState.vue'
import MockNetworkPreviewChart from '@/components/MockNetworkPreviewChart.vue'
import StatusDot from '@/components/StatusDot.vue'
import { useAppStore } from '@/stores/app'
import type { MockConfig, MockDirectionConfig, UiExtension } from '@/types/contracts'
import { cloneData } from '@/utils/format'

defineProps<{ extension?: UiExtension }>()

const store = useAppStore()
const message = useMessage()
const tab = ref('network')
const draft = ref<MockConfig | null>(store.mockConfig ? cloneData(store.mockConfig) : null)

watch(
  () => store.mockConfig,
  (value) => {
    draft.value = value ? cloneData(value) : null
  },
  { deep: true },
)

const linkEntries = computed(() => Object.entries(draft.value?.links || {}))
const aEntry = computed(() => linkEntries.value.find(([key]) => /agent-a.*agent-b/i.test(key)) || linkEntries.value[0])
const bEntry = computed(() => linkEntries.value.find(([key]) => /agent-b.*agent-a/i.test(key)) || linkEntries.value[1])
const aToB = computed(() => aEntry.value?.[1])
const bToA = computed(() => bEntry.value?.[1])
const dirty = computed(() => JSON.stringify(draft.value) !== JSON.stringify(store.mockConfig))
const seed = computed(() => {
  const model = draft.value?.model || draft.value?.model_behavior
  return model?.deterministic_seed ?? model?.response_seed ?? aToB.value?.seed
})

const modelSeedKey = computed(() => {
  if (draft.value?.model_behavior.deterministic_seed !== undefined) return 'deterministic_seed'
  if (draft.value?.model_behavior.response_seed !== undefined) return 'response_seed'
  return null
})

function patchLink(key: string | undefined, field: keyof MockDirectionConfig, value: number | null): void {
  if (!draft.value?.links || !key || value === null) return
  draft.value.links[key] = { ...draft.value.links[key]!, [field]: value }
  syncScenario()
}

function patchModel(field: string, value: string | number | null): void {
  if (!draft.value || value === null) return
  const current = draft.value.model || draft.value.model_behavior
  const next = { ...current, [field]: value }
  draft.value.model = next
  draft.value.model_behavior = next
}

function syncScenario(): void {
  if (!draft.value || !aToB.value || !bToA.value) return
  draft.value.scenarios = [{
    id: 'current',
    name: '当前配置',
    a_to_b: aToB.value,
    b_to_a: bToA.value,
  }]
}

function restore(): void {
  draft.value = store.mockConfig ? cloneData(store.mockConfig) : null
}

async function save(): Promise<void> {
  if (!draft.value) return
  if (await store.saveMock(draft.value)) message.success('Mock 配置已保存，将从下一次交接生效')
}

async function copySeed(): Promise<void> {
  if (seed.value === undefined) return
  await navigator.clipboard.writeText(String(seed.value))
  message.success('种子已复制')
}
</script>

<template>
  <div class="page-shell mock-page">
    <nav class="breadcrumbs" aria-label="面包屑">
      <RouterLink to="/environment">环境与适配器</RouterLink><ChevronRight :size="14" />
      <span>{{ store.environment?.name || '当前环境' }}</span><ChevronRight :size="14" />
      <strong>Mock 实验室</strong>
    </nav>

    <header class="mock-heading">
      <div class="mock-title-block">
        <h1>Mock 实验室</h1>
        <span>环境：<strong>{{ store.environment?.name || '—' }}</strong></span>
        <span>配置版本：<strong>v{{ draft?.version ?? '—' }}</strong></span>
        <span>种子：<strong class="mono">{{ seed ?? '—' }}</strong><button v-if="seed !== undefined" type="button" aria-label="复制种子" @click="copySeed"><Copy :size="13" /></button></span>
        <span class="extension-source"><FlaskConical :size="16" />由适配器扩展提供</span>
      </div>
      <div class="mock-actions">
        <NButton :disabled="!draft" @click="message.info('预览已随输入实时更新')"><template #icon><NIcon><Eye /></NIcon></template>预览</NButton>
        <NButton type="primary" :disabled="!dirty" :loading="store.actionPending === 'save-mock'" @click="save">应用到下一次交接</NButton>
        <NButton :disabled="!dirty" @click="restore"><template #icon><NIcon><RefreshCcw /></NIcon></template>恢复已保存</NButton>
      </div>
    </header>

    <EmptyState v-if="!draft" title="Mock 配置不可用" description="该扩展入口已注册，但 Coordinator 未返回 /api/v1/mock 配置。" />
    <NTabs v-else v-model:value="tab" type="line" size="large" class="mock-tabs" animated>
      <NTabPane name="network" tab="网络场景">
        <div v-if="aToB && bToA" class="network-layout">
          <aside class="surface scenario-list">
            <div class="scenario-list__header"><h2>场景列表</h2></div>
            <button type="button" class="scenario-row scenario-row--active">
              <StatusDot tone="success" /><span><strong>当前配置</strong><small>双向链路参数</small></span>
            </button>
            <p>本轮由 Coordinator 保存一份当前场景；版本更新从下一次交接生效。</p>
          </aside>

          <div class="network-editor">
            <section class="surface direction-card">
              <h2>A → B（从 A 到 B）<span><StatusDot tone="success" />已启用</span></h2>
              <div class="direction-fields">
                <label><span>带宽（Bandwidth）</span><NInputNumber :value="aToB.bandwidth_mbps" :min="0.001" @update:value="patchLink(aEntry?.[0], 'bandwidth_mbps', $event)"><template #suffix>Mbps</template></NInputNumber></label>
                <label><span>RTT</span><NInputNumber :value="aToB.rtt_ms" :min="0" @update:value="patchLink(aEntry?.[0], 'rtt_ms', $event)"><template #suffix>ms</template></NInputNumber></label>
                <label><span>抖动（Jitter）</span><NInputNumber :value="aToB.jitter_ms" :min="0" @update:value="patchLink(aEntry?.[0], 'jitter_ms', $event)"><template #suffix>ms</template></NInputNumber></label>
                <label><span>方向种子</span><NInputNumber :value="aToB.seed" :precision="0" @update:value="patchLink(aEntry?.[0], 'seed', $event)" /></label>
              </div>
              <div class="valid-line"><CheckCircle2 :size="14" />参数有效</div>
            </section>

            <section class="surface direction-card">
              <h2>B → A（从 B 到 A）<span><StatusDot tone="success" />已启用</span></h2>
              <div class="direction-fields">
                <label><span>带宽（Bandwidth）</span><NInputNumber :value="bToA.bandwidth_mbps" :min="0.001" @update:value="patchLink(bEntry?.[0], 'bandwidth_mbps', $event)"><template #suffix>Mbps</template></NInputNumber></label>
                <label><span>RTT</span><NInputNumber :value="bToA.rtt_ms" :min="0" @update:value="patchLink(bEntry?.[0], 'rtt_ms', $event)"><template #suffix>ms</template></NInputNumber></label>
                <label><span>抖动（Jitter）</span><NInputNumber :value="bToA.jitter_ms" :min="0" @update:value="patchLink(bEntry?.[0], 'jitter_ms', $event)"><template #suffix>ms</template></NInputNumber></label>
                <label><span>方向种子</span><NInputNumber :value="bToA.seed" :precision="0" @update:value="patchLink(bEntry?.[0], 'seed', $event)" /></label>
              </div>
              <div class="valid-line"><CheckCircle2 :size="14" />参数有效</div>
            </section>
          </div>

          <section class="surface preview-panel">
            <div class="surface-header"><h2 class="surface-title">场景预览</h2><span>当前双向配置</span></div>
            <MockNetworkPreviewChart :a-to-b="aToB" :b-to-a="bToA" :height="410" />
            <div class="preview-note"><Info :size="16" />带宽、RTT 与抖动使用各自量纲；图表只展示当前输入，不代表实测结果。</div>
          </section>
        </div>
        <EmptyState v-else title="缺少双向链路配置" description="Mock Adapter 必须提供两个方向的 links 配置。" />
      </NTabPane>

      <NTabPane name="model" tab="模型行为">
        <div class="model-layout">
          <section class="surface model-editor">
            <div class="surface-header"><h2 class="surface-title">确定性模型行为</h2><span class="model-provider mono">{{ store.capabilities.providers?.model || '—' }}</span></div>
            <div class="model-fields">
              <label><span>Tokenize 延迟</span><NInputNumber :value="draft.model_behavior.tokenize_delay_ms" :min="0" @update:value="patchModel('tokenize_delay_ms', $event)"><template #suffix>ms</template></NInputNumber></label>
              <label><span>Prefill 延迟</span><NInputNumber :value="draft.model_behavior.prefill_delay_ms" :min="0" @update:value="patchModel('prefill_delay_ms', $event)"><template #suffix>ms</template></NInputNumber></label>
              <label><span>Prefill 每 Token 延迟</span><NInputNumber :value="draft.model_behavior.prefill_per_token_ms ?? 0" :min="0" :max="10" @update:value="patchModel('prefill_per_token_ms', $event)"><template #suffix>ms</template></NInputNumber></label>
              <label><span>KV 导出延迟</span><NInputNumber :value="draft.model_behavior.cache_export_delay_ms ?? 0" :min="0" @update:value="patchModel('cache_export_delay_ms', $event)"><template #suffix>ms</template></NInputNumber></label>
              <label><span>KV 导入延迟</span><NInputNumber :value="draft.model_behavior.cache_import_delay_ms ?? 0" :min="0" @update:value="patchModel('cache_import_delay_ms', $event)"><template #suffix>ms</template></NInputNumber></label>
              <label><span>Generate 延迟</span><NInputNumber :value="draft.model_behavior.generate_delay_ms" :min="0" @update:value="patchModel('generate_delay_ms', $event)"><template #suffix>ms</template></NInputNumber></label>
              <label v-if="modelSeedKey"><span>确定性种子</span><NInputNumber :value="Number(draft.model_behavior[modelSeedKey] ?? 0)" :precision="0" @update:value="patchModel(modelSeedKey, $event)" /></label>
              <label v-if="draft.model_behavior.response_prefix !== undefined" class="prefix-field"><span>响应前缀</span><NInput :value="draft.model_behavior.response_prefix" @update:value="patchModel('response_prefix', $event)" /></label>
            </div>
          </section>
          <section class="surface behavior-note">
            <Target :size="25" />
            <div><h2>可重复实验</h2><p>相同输入、Agent Profile、逻辑轮次与确定性种子应得到稳定输出；A/B 仍使用隔离的逻辑模型 Session。</p></div>
            <span><Hash :size="18" />{{ seed ?? '—' }}</span>
          </section>
        </div>
      </NTabPane>
    </NTabs>

    <footer v-if="draft" class="mock-footer">
      <span><CheckCircle2 :size="19" />配置校验：<strong>通过</strong></span>
      <span><Target :size="19" />生效范围：<strong>下一次交接</strong></span>
      <span><Hash :size="19" />确定性种子：<strong>{{ seed ?? '—' }}</strong></span>
      <span v-if="dirty" class="unsaved"><StatusDot tone="warning" />存在未保存的更改</span>
      <NButton type="primary" :disabled="!dirty" :loading="store.actionPending === 'save-mock'" @click="save"><template #icon><NIcon><Save /></NIcon></template>保存配置</NButton>
    </footer>
  </div>
</template>

<style scoped>
.mock-page { padding-top: 15px; }
.breadcrumbs { display: flex; height: 28px; align-items: center; gap: 7px; color: var(--color-text-muted); font-size: 11px; }
.breadcrumbs a:hover { color: var(--color-primary); }
.breadcrumbs strong { color: var(--color-text-secondary); font-weight: 500; }
.mock-heading { display: flex; min-height: 78px; align-items: center; justify-content: space-between; gap: 20px; }
.mock-title-block { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.mock-title-block h1 { margin: 0 6px 0 0; font-size: 26px; }
.mock-title-block > span { display: inline-flex; min-height: 37px; padding: 0 10px; align-items: center; gap: 5px; color: var(--color-text-muted); background: var(--color-surface-subtle); border: 1px solid var(--color-border-soft); border-radius: 5px; font-size: 11px; }
.mock-title-block strong { color: var(--color-primary); font-weight: 600; }
.mock-title-block button { display: grid; padding: 2px; color: var(--color-text-muted); cursor: pointer; background: none; border: 0; place-items: center; }
.extension-source { color: #60708d !important; background: #eef4ff !important; }
.mock-actions { display: flex; gap: 10px; }
.mock-tabs :deep(.n-tabs-nav) { border-bottom: 1px solid var(--color-border); }
.mock-tabs :deep(.n-tabs-tab) { min-width: 112px; justify-content: center; }
.mock-tabs :deep(.n-tabs-pane-wrapper) { padding-top: 16px; }
.network-layout { display: grid; grid-template-columns: 252px minmax(470px, 1.15fr) minmax(400px, 1fr); gap: 14px; }
.scenario-list { overflow: hidden; box-shadow: none; }
.scenario-list__header { display: flex; min-height: 50px; padding: 0 14px; align-items: center; border-bottom: 1px solid var(--color-border-soft); }
.scenario-list h2 { margin: 0; font-size: 14px; }
.scenario-row { display: grid; width: 100%; min-height: 72px; padding: 10px 14px; align-items: center; grid-template-columns: auto 1fr; gap: 10px; color: var(--color-text-secondary); cursor: default; background: #fff; border: 0; border-bottom: 1px solid var(--color-border-soft); text-align: left; }
.scenario-row--active { color: var(--color-primary); background: #eef4ff; box-shadow: inset -3px 0 var(--color-primary); }
.scenario-row span { display: flex; flex-direction: column; gap: 4px; }
.scenario-row strong { font-size: 13px; }
.scenario-row small { color: var(--color-text-muted); font-size: 10px; }
.scenario-list > p { padding: 14px; margin: 0; color: var(--color-text-muted); font-size: 10px; line-height: 1.6; }
.network-editor { display: flex; flex-direction: column; gap: 10px; }
.direction-card { padding: 15px 18px; box-shadow: none; }
.direction-card h2 { display: flex; margin: 0 0 13px; align-items: center; justify-content: space-between; font-size: 15px; }
.direction-card h2 span { display: inline-flex; align-items: center; gap: 6px; color: var(--color-text-secondary); font-size: 10px; font-weight: 500; }
.direction-fields { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(180px, 100%), 1fr)); gap: 12px; }
.direction-fields label, .model-fields label { display: flex; min-width: 0; flex-direction: column; gap: 6px; }
.direction-fields label > span, .model-fields label > span { color: var(--color-text-secondary); font-size: 10px; font-weight: 600; }
:deep(.n-input-number) { width: 100%; }
.valid-line { display: flex; margin-top: 11px; align-items: center; gap: 6px; color: var(--color-success); font-size: 10px; }
.preview-panel { overflow: hidden; box-shadow: none; }
.preview-panel .surface-header > span { color: var(--color-text-muted); font-size: 10px; }
.preview-note { display: flex; min-height: 48px; padding: 10px 14px; align-items: center; gap: 8px; color: var(--color-text-muted); background: var(--color-surface-subtle); border-top: 1px solid var(--color-border-soft); font-size: 10px; }
.model-layout { display: grid; grid-template-columns: minmax(0, 1fr) 360px; gap: 14px; }
.model-editor { overflow: hidden; box-shadow: none; }
.model-provider { color: var(--color-text-muted); font-size: 11px; }
.model-fields { display: grid; padding: 22px; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; }
.prefix-field { grid-column: 1 / -1; }
.behavior-note { display: flex; padding: 24px; align-items: flex-start; gap: 13px; color: var(--color-primary); box-shadow: none; }
.behavior-note h2 { margin: 0; color: var(--color-text); font-size: 15px; }
.behavior-note p { margin: 7px 0 0; color: var(--color-text-secondary); font-size: 11px; line-height: 1.7; }
.behavior-note > span { display: flex; padding: 7px 9px; margin-left: auto; align-items: center; gap: 5px; color: var(--color-text-secondary); background: var(--color-surface-subtle); border-radius: 5px; font-size: 11px; }
.mock-footer { display: flex; min-height: 76px; padding: 13px 18px; margin-top: 16px; align-items: center; justify-content: space-between; gap: 18px; color: var(--color-success); background: #f1faf4; border: 1px solid #cdebd7; border-radius: 7px; }
.mock-footer > span { display: inline-flex; align-items: center; gap: 7px; color: var(--color-text-secondary); font-size: 11px; }
.mock-footer strong { color: var(--color-success); }
.mock-footer .unsaved { margin-left: auto; color: var(--color-text-muted); }
@media (max-width: 1300px) {
  .mock-heading { align-items: flex-start; flex-direction: column; padding: 10px 0 16px; }
  .network-layout { grid-template-columns: 220px minmax(0, 1fr); }
  .preview-panel { grid-column: 1 / -1; }
}
@media (max-width: 820px) {
  .network-layout, .model-layout { grid-template-columns: 1fr; }
  .scenario-list { display: none; }
  .preview-panel { grid-column: auto; }
  .model-fields { grid-template-columns: 1fr 1fr; }
  .mock-footer { align-items: flex-start; flex-wrap: wrap; justify-content: flex-start; }
  .mock-footer .unsaved { margin-left: 0; }
}
@media (max-width: 600px) {
  .mock-actions { width: 100%; flex-wrap: wrap; }
  .mock-actions :deep(.n-button) { flex: 1; }
  .model-fields { grid-template-columns: 1fr 1fr; }
  .mock-title-block h1 { width: 100%; }
  .breadcrumbs { overflow-x: auto; white-space: nowrap; }
}
</style>
