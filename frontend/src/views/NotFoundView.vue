<script setup lang="ts">
import { NButton, NResult } from 'naive-ui'
</script>

<template>
  <div class="page-shell not-found">
    <NResult status="404" title="页面不存在" description="该页面未注册，或对应的适配器扩展当前不可用。">
      <template #footer>
        <NButton type="primary" tag="a" href="/sessions">返回会话</NButton>
      </template>
    </NResult>
  </div>
</template>

<style scoped>
.not-found {
  display: grid;
  min-height: calc(100vh - var(--header-height));
  place-items: center;
}
</style>
