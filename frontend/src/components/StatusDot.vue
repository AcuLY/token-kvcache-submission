<script setup lang="ts">
withDefaults(
  defineProps<{
    tone?: 'success' | 'primary' | 'warning' | 'danger' | 'muted'
    pulse?: boolean
  }>(),
  { tone: 'muted', pulse: false },
)
</script>

<template>
  <span class="status-dot" :class="[`status-dot--${tone}`, { 'status-dot--pulse': pulse }]" aria-hidden="true" />
</template>

<style scoped>
.status-dot {
  display: inline-block;
  flex: 0 0 auto;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #9aa4b5;
}

.status-dot--success { background: var(--color-success); }
.status-dot--primary { background: var(--color-primary); }
.status-dot--warning { background: var(--color-warning); }
.status-dot--danger { background: var(--color-danger); }
.status-dot--muted { background: #9aa4b5; }

.status-dot--pulse {
  box-shadow: 0 0 0 4px rgb(20 93 255 / 10%);
  animation: status-pulse 2s ease-out infinite;
}

@keyframes status-pulse {
  50% { box-shadow: 0 0 0 7px rgb(20 93 255 / 0%); }
}
</style>
