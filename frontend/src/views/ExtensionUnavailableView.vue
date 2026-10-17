<script setup lang="ts">
import type { UiExtension } from '@/types/contracts'
import EmptyState from '@/components/EmptyState.vue'

defineProps<{ extension: UiExtension }>()
</script>

<template>
  <div class="page-shell">
    <div class="page-heading">
      <div>
        <h1>{{ extension.label }}</h1>
        <p>此工具由当前适配器声明。</p>
      </div>
    </div>
    <div class="surface">
      <EmptyState
        kind="unavailable"
        title="扩展视图尚未安装"
        description="CapabilityDocument 已声明入口，但当前 WebUI 不包含对应的渲染器。"
      />
    </div>
  </div>
</template>
