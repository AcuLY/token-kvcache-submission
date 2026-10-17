<script setup lang="ts">
import { computed } from 'vue'
import VChart from 'vue-echarts'
import type { ObservationEvent, TransferRecord } from '@/types/contracts'
import EmptyState from './EmptyState.vue'
import '@/charts/setup'

const props = withDefaults(
  defineProps<{
    transfers?: TransferRecord[]
    events?: ObservationEvent[]
    height?: number
    compact?: boolean
  }>(),
  { transfers: () => [], events: () => [], height: 270, compact: false },
)

interface NetworkPoint {
  label: string
  direction: string
  bandwidth?: number
  rtt?: number
}

const points = computed<NetworkPoint[]>(() => {
  const fromTransfers = props.transfers
    .filter((item) => item.network)
    .map((item, index) => ({
      label: `T${item.turn_index ?? item.turn_id ?? index + 1}`,
      direction: item.direction || `${item.source_agent_id || '?'} → ${item.target_agent_id || '?'}`,
      bandwidth: item.network?.bandwidth_mbps,
      rtt: item.network?.rtt_ms,
    }))

  if (fromTransfers.length) return fromTransfers
  return props.events
    .filter((event) => event.type.toLowerCase().includes('network'))
    .map((event) => {
      const payload = event.payload || {}
      const nested = payload.network && typeof payload.network === 'object' && !Array.isArray(payload.network)
        ? payload.network as Record<string, unknown>
        : payload as Record<string, unknown>
      return {
        label: `#${event.seq}`,
        direction: String(nested.direction || ''),
        bandwidth: typeof nested.bandwidth_mbps === 'number' ? nested.bandwidth_mbps : undefined,
        rtt: typeof nested.rtt_ms === 'number' ? nested.rtt_ms : undefined,
      }
    })
    .filter((item) => item.bandwidth !== undefined || item.rtt !== undefined)
})

const option = computed(() => ({
  animationDuration: 260,
  color: ['#145DFF', '#FF8B1F'],
  tooltip: {
    trigger: 'axis',
    backgroundColor: '#0B1739',
    borderWidth: 0,
    textStyle: { color: '#FFFFFF', fontSize: 12 },
  },
  legend: {
    bottom: 0,
    itemWidth: 18,
    itemHeight: 3,
    textStyle: { color: '#5C6880', fontSize: 11 },
  },
  grid: { left: 46, right: 48, top: 26, bottom: 48 },
  xAxis: {
    type: 'category',
    data: points.value.map((item) => item.label),
    boundaryGap: false,
    axisLine: { lineStyle: { color: '#C9D2E2' } },
    axisTick: { show: false },
    axisLabel: { color: '#69758D', fontSize: 10 },
  },
  yAxis: [
    {
      type: 'value',
      name: 'Mbps',
      nameTextStyle: { color: '#145DFF', fontSize: 10 },
      splitLine: { lineStyle: { color: '#E8EDF5', type: 'dashed' } },
      axisLabel: { color: '#69758D', fontSize: 10 },
    },
    {
      type: 'value',
      name: 'RTT (ms)',
      nameTextStyle: { color: '#FF8B1F', fontSize: 10 },
      splitLine: { show: false },
      axisLabel: { color: '#69758D', fontSize: 10 },
    },
  ],
  series: [
    {
      name: '带宽 (Mbps)',
      type: 'line',
      data: points.value.map((item) => item.bandwidth ?? null),
      showSymbol: points.value.length < 20,
      symbolSize: 6,
      lineStyle: { width: 2 },
      areaStyle: props.compact ? undefined : { color: 'rgba(20, 93, 255, 0.05)' },
    },
    {
      name: 'RTT (ms)',
      type: 'line',
      yAxisIndex: 1,
      data: points.value.map((item) => item.rtt ?? null),
      showSymbol: points.value.length < 20,
      symbolSize: 6,
      lineStyle: { width: 2, type: 'dashed' },
    },
  ],
}))
</script>

<template>
  <div class="network-chart" :style="{ minHeight: `${height}px` }">
    <EmptyState
      v-if="!points.length"
      compact
      title="暂无网络观测"
      description="网络快照随 Token 交接产生；图表不会用模拟零值补齐。"
    />
    <VChart v-else :option="option" autoresize :style="{ height: `${height}px` }" />
  </div>
</template>

<style scoped>
.network-chart {
  width: 100%;
}
</style>
