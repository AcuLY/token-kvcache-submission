<script setup lang="ts">
import { computed } from 'vue'
import { Bot } from 'lucide-vue-next'
import { NInput, NInputNumber } from 'naive-ui'
import { mergeProfilePrompt } from '@/utils/prompts'
import type { AgentProfile } from '@/types/contracts'

const props = withDefaults(defineProps<{ modelValue: AgentProfile; accent?: 'a' | 'b'; label: string }>(), { accent: 'a' })
const emit = defineEmits<{ 'update:modelValue': [value: AgentProfile] }>()

const maxTokenKey = computed(() =>
  Object.prototype.hasOwnProperty.call(props.modelValue.generation_config, 'max_new_tokens') ? 'max_new_tokens' : 'max_output_tokens',
)

const prompt = computed(() => mergeProfilePrompt(props.modelValue).system_prompt)

function patchPrompt(value: string): void {
  emit('update:modelValue', { ...mergeProfilePrompt(props.modelValue), system_prompt: value })
}

function patch(field: keyof AgentProfile, value: unknown): void {
  emit('update:modelValue', { ...props.modelValue, [field]: value })
}

function patchGeneration(field: string, value: number | null): void {
  emit('update:modelValue', {
    ...props.modelValue,
    generation_config: { ...props.modelValue.generation_config, [field]: value ?? undefined },
  })
}
</script>

<template>
  <section class="profile-editor" :class="`profile-editor--${accent}`">
    <h3><span><Bot :size="15" /></span>{{ label }}</h3>
    <div class="profile-grid">
      <label>
        <span>显示名称</span>
        <NInput :value="modelValue.display_name" @update:value="patch('display_name', $event)" />
      </label>
      <label class="system-prompt">
        <span>提示词</span>
        <NInput :value="prompt" type="textarea" :autosize="{ minRows: 8, maxRows: 16 }" placeholder="写清这个 Agent 的身份、立场和回复要求。" @update:value="patchPrompt" />
      </label>
      <details class="generation-settings">
        <summary>生成参数</summary>
      <div class="generation-fields">
        <label>
          <span>温度</span>
          <NInputNumber :value="Number(modelValue.generation_config.temperature ?? 0)" :min="0" :max="2" :step="0.1" @update:value="patchGeneration('temperature', $event)" />
        </label>
        <label v-if="modelValue.generation_config.top_p !== undefined">
          <span>Top-p</span>
          <NInputNumber :value="Number(modelValue.generation_config.top_p)" :min="0" :max="1" :step="0.05" @update:value="patchGeneration('top_p', $event)" />
        </label>
        <label>
          <span>最大输出 Tokens</span>
          <NInputNumber :value="Number(modelValue.generation_config[maxTokenKey] ?? 0)" :min="1" :precision="0" @update:value="patchGeneration(maxTokenKey, $event)" />
        </label>
      </div>
      </details>
    </div>
  </section>
</template>

<style scoped>
.profile-editor { min-width: 0; padding: 0 16px 4px; }
.profile-editor + .profile-editor { border-left: 1px solid var(--color-border); }
h3 { display: flex; margin: 0 0 13px; align-items: center; gap: 8px; font-size: 13px; }
h3 span { display: grid; width: 25px; height: 25px; color: #fff; border-radius: 50%; place-items: center; }
.profile-editor--a h3 span { background: var(--color-cyan); }
.profile-editor--b h3 span { background: var(--color-orange); }
.profile-grid { display: grid; grid-template-columns: minmax(0, 1fr); gap: 12px; }
label { display: flex; min-width: 0; flex-direction: column; gap: 5px; }
label > span { color: var(--color-text-secondary); font-size: 11px; font-weight: 600; }
.generation-settings summary { cursor: pointer; color: var(--color-text-secondary); font-size: 12px; }
.generation-settings[open] summary { margin-bottom: 12px; }
.generation-fields { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.generation-fields { grid-column: 1 / -1; grid-template-columns: repeat(2, minmax(0, 1fr)); padding-top: 10px; border-top: 1px solid var(--color-border-soft); }
:deep(.n-input-number) { width: 100%; }
@media (max-width: 750px) {
  .profile-editor { padding-inline: 0; }
  .profile-editor + .profile-editor { padding-top: 18px; border-top: 1px solid var(--color-border); border-left: 0; }
  .profile-grid { grid-template-columns: 1fr; }
  .system-prompt { grid-column: auto; grid-row: auto; }
  .generation-fields { grid-column: auto; grid-template-columns: 1fr; }
}
</style>
