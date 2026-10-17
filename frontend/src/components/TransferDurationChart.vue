<script setup lang="ts">
import { computed } from 'vue'
import VChart from 'vue-echarts'
import type { TransferRecord } from '@/types/contracts'
import EmptyState from './EmptyState.vue'
import '@/charts/setup'

const props = withDefaults(defineProps<{ transfers?: TransferRecord[]; height?: number }>(), { transfers: () => [], height: 280 })

const usable = computed(() => props.transfers.filter((item) => item.actual_ms !== undefined || item.predicted_ms !== undefined))
const option = computed(() => ({
  animationDuration: 260,
  color: ['#145DFF', '#72A0FF'],
  tooltip: { trigger: 'axis', backgroundColor: '#0B1739', borderWidth: 0, textStyle: { color: '#fff', fontSize: 12 } },
  legend: { bottom: 0, textStyle: { color: '#5C6880', fontSize: 11 } },
  grid: { left: 48, right: 20, top: 24, bottom: 48 },
  xAxis: {
    type: 'category',
    data: usable.value.map((item, index) => `T${item.turn_index ?? item.turn_id ?? index + 1}`),
    axisTick: { show: false },
    axisLine: { lineStyle: { color: '#C9D2E2' } },
    axisLabel: { color: '#69758D', fontSize: 10 },
  },
  yAxis: {
    type: 'value',
    name: '耗时 (ms)',
    nameTextStyle: { color: '#69758D', fontSize: 10 },
    splitLine: { lineStyle: { color: '#E8EDF5', type: 'dashed' } },
    axisLabel: { color: '#69758D', fontSize: 10 },
  },
  series: [
    {
      name: '实际耗时',
      type: 'line',
      data: usable.value.map((item) => item.actual_ms ?? item.timings?.total_ms ?? null),
      symbol: 'circle',
      symbolSize: 7,
      lineStyle: { width: 2.5 },
    },
    {
      name: '预测耗时',
      type: 'line',
      data: usable.value.map((item) => item.predicted_ms ?? null),
      symbol: 'emptyCircle',
      symbolSize: 6,
      lineStyle: { width: 1.5, type: 'dashed' },
    },
  ],
}))
</script>

<template>
  <div :style="{ minHeight: `${height}px` }">
    <EmptyState v-if="!usable.length" compact title="暂无交接耗时" description="有实测或预测耗时后才绘制曲线，不用零值代替缺失数据。" />
    <VChart v-else :option="option" autoresize :style="{ height: `${height}px` }" />
  </div>
</template>
