<script setup lang="ts">
import { Inbox, CircleOff } from 'lucide-vue-next'

withDefaults(
  defineProps<{
    title: string
    description?: string
    kind?: 'empty' | 'unavailable'
    compact?: boolean
  }>(),
  { description: '', kind: 'empty', compact: false },
)
</script>

<template>
  <div class="empty-state" :class="{ 'empty-state--compact': compact }" role="status">
    <component :is="kind === 'unavailable' ? CircleOff : Inbox" :size="compact ? 24 : 30" :stroke-width="1.6" />
    <div>
      <p class="empty-state__title">{{ title }}</p>
      <p v-if="description" class="empty-state__description">{{ description }}</p>
    </div>
    <slot />
  </div>
</template>

<style scoped>
.empty-state {
  display: flex;
  min-height: 180px;
  padding: 28px;
  align-items: center;
  justify-content: center;
  flex-direction: column;
  gap: 12px;
  color: #8a96ab;
  text-align: center;
}

.empty-state--compact {
  min-height: 112px;
  padding: 18px;
  flex-direction: row;
  text-align: left;
}

.empty-state__title {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 15px;
  font-weight: 600;
}

.empty-state__description {
  max-width: 540px;
  margin: 5px 0 0;
  color: var(--color-text-muted);
  font-size: 13px;
  line-height: 1.6;
}
</style>
