<script setup lang="ts">
import { Activity, ArrowRight } from 'lucide-vue-next'
import type { ObservationEvent } from '@/types/contracts'
import EmptyState from './EmptyState.vue'
import { eventTypeLabel, formatClock } from '@/utils/format'

withDefaults(defineProps<{ events?: ObservationEvent[]; limit?: number }>(), { events: () => [], limit: 8 })

function eventDetail(event: ObservationEvent): string {
  const payload = event.payload || {}
  const detail = payload.message || payload.detail || payload.reason || payload.status
  if (typeof detail === 'string') return detail
  const ids = [event.agent_id, event.transfer_id, event.turn_id !== undefined ? `Turn ${event.turn_id}` : undefined]
  return ids.filter(Boolean).join(' · ')
}
</script>

<template>
  <div class="event-list">
    <EmptyState v-if="!events.length" compact title="暂无事件" description="实时事件到达后会按 seq 顺序显示。" />
    <ol v-else>
      <li v-for="event in events.slice(-limit).reverse()" :key="event.seq">
        <span class="event-icon"><Activity :size="15" /></span>
        <span class="event-main">
          <strong>{{ eventTypeLabel(event.type) }}</strong>
          <small v-if="eventDetail(event)" class="truncate">{{ eventDetail(event) }}</small>
        </span>
        <span class="event-seq mono">#{{ event.seq }}</span>
        <time>{{ formatClock(event.timestamp) }}</time>
        <ArrowRight :size="14" class="event-arrow" />
      </li>
    </ol>
  </div>
</template>

<style scoped>
ol { padding: 0; margin: 0; list-style: none; }
li { display: grid; min-height: 48px; padding: 8px 16px; align-items: center; grid-template-columns: auto minmax(0, 1fr) auto auto auto; gap: 10px; border-bottom: 1px solid var(--color-border-soft); }
li:last-child { border-bottom: 0; }
.event-icon { display: grid; width: 28px; height: 28px; color: var(--color-primary); background: var(--color-primary-soft); border-radius: 50%; place-items: center; }
.event-main { display: flex; min-width: 0; flex-direction: column; gap: 2px; }
.event-main strong { color: var(--color-text-secondary); font-size: 12px; font-weight: 600; }
.event-main small, time, .event-seq { color: var(--color-text-muted); font-size: 10px; }
.event-arrow { color: #a1aaba; }
@media (max-width: 540px) {
  li { grid-template-columns: auto minmax(0, 1fr) auto; }
  time, .event-arrow { display: none; }
}
</style>
