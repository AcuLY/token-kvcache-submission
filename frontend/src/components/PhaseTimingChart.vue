<script setup lang="ts">
import { computed } from 'vue'
import VChart from 'vue-echarts'
import type { TransferRecord } from '@/types/contracts'
import EmptyState from './EmptyState.vue'
import '@/charts/setup'

const props = withDefaults(defineProps<{ transfers?: TransferRecord[]; height?: number }>(), { transfers: () => [], height: 250 })

const usable = computed(() => props.transfers.filter((item) => item.timings && Object.keys(item.timings).some((key) => key !== 'total_ms')))
const phases: { key: string; fallback?: string; label: string; color: string }[] = [
  { key: 'sender_prepare_ms', fallback: 'prepare_ms', label: '准备', color: '#316FEA' },
  { key: 'p2p_ms', label: 'P2P 传输', color: '#24ABC0' },
  { key: 'receiver_prefill_ms', label: '接收端 Prefill', color: '#F5B43B' },
  { key: 'receiver_import_ms', label: 'KV 导入', color: '#E28A52' },
  { key: 'context_rebuild_ms', label: '窗口重建', color: '#A67859' },
  { key: 'local_prefill_ms', label: '本地 Prefill', color: '#8B6BD9' },
  { key: 'generation_ms', label: '生成', color: '#65B92E' },
]

const option = computed(() => ({
  animationDuration: 260,
  tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, backgroundColor: '#0B1739', borderWidth: 0, textStyle: { color: '#fff', fontSize: 12 } },
  legend: { bottom: 0, textStyle: { color: '#5C6880', fontSize: 10 } },
  grid: { left: 70, right: 32, top: 15, bottom: 50 },
  xAxis: { type: 'value', splitLine: { lineStyle: { color: '#E8EDF5', type: 'dashed' } }, axisLabel: { color: '#69758D', fontSize: 10 } },
  yAxis: {
    type: 'category',
    data: usable.value.slice(-6).map((item, index) => `T${item.turn_index ?? item.turn_id ?? index + 1}`),
    axisTick: { show: false },
    axisLine: { show: false },
    axisLabel: { color: '#36415A', fontSize: 10 },
  },
  series: phases.map((phase) => ({
    name: phase.label,
    type: 'bar',
    stack: 'total',
    barMaxWidth: 20,
    itemStyle: { color: phase.color, borderColor: '#fff', borderWidth: 0.5 },
    data: usable.value.slice(-6).map((item) => {
      const value = item.timings?.[phase.key]
      return value ?? (phase.fallback ? item.timings?.[phase.fallback] : undefined) ?? null
    }),
  })),
}))
</script>

<template>
  <div :style="{ minHeight: `${height}px` }">
    <EmptyState v-if="!usable.length" compact title="暂无分阶段耗时" description="等待 Agent 上报 prepare、P2P、prefill 与 generation 指标。" />
    <VChart v-else :option="option" autoresize :style="{ height: `${height}px` }" />
  </div>
</template>
