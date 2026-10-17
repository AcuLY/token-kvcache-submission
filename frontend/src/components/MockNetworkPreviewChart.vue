<script setup lang="ts">
import { computed } from 'vue'
import VChart from 'vue-echarts'
import type { MockDirectionConfig } from '@/types/contracts'
import '@/charts/setup'

const props = defineProps<{
  aToB: MockDirectionConfig
  bToA: MockDirectionConfig
  height?: number
}>()

const option = computed(() => ({
  animationDuration: 220,
  color: ['#145DFF', '#FF8B1F'],
  tooltip: {
    trigger: 'axis',
    backgroundColor: '#0B1739',
    borderWidth: 0,
    textStyle: { color: '#FFFFFF', fontSize: 12 },
  },
  legend: { bottom: 0, textStyle: { color: '#5C6880', fontSize: 11 } },
  grid: { left: 50, right: 50, top: 34, bottom: 52 },
  xAxis: {
    type: 'category',
    data: ['A → B', 'B → A'],
    axisTick: { show: false },
    axisLine: { lineStyle: { color: '#C9D2E2' } },
    axisLabel: { color: '#44506A', fontSize: 11 },
  },
  yAxis: [
    {
      type: 'value',
      name: '带宽 (Mbps)',
      min: 0,
      nameTextStyle: { color: '#145DFF', fontSize: 10 },
      splitLine: { lineStyle: { color: '#E8EDF5', type: 'dashed' } },
      axisLabel: { color: '#69758D', fontSize: 10 },
    },
    {
      type: 'value',
      name: '延迟 (ms)',
      min: 0,
      nameTextStyle: { color: '#FF8B1F', fontSize: 10 },
      splitLine: { show: false },
      axisLabel: { color: '#69758D', fontSize: 10 },
    },
  ],
  series: [
    {
      name: '带宽',
      type: 'bar',
      barMaxWidth: 54,
      data: [props.aToB.bandwidth_mbps, props.bToA.bandwidth_mbps],
      itemStyle: { color: '#145DFF', borderRadius: [4, 4, 0, 0] },
    },
    {
      name: 'RTT',
      type: 'line',
      yAxisIndex: 1,
      symbolSize: 8,
      data: [props.aToB.rtt_ms, props.bToA.rtt_ms],
      lineStyle: { width: 2, color: '#FF8B1F' },
      itemStyle: { color: '#FF8B1F' },
    },
    {
      name: '抖动',
      type: 'line',
      yAxisIndex: 1,
      symbol: 'emptyCircle',
      symbolSize: 7,
      data: [props.aToB.jitter_ms, props.bToA.jitter_ms],
      lineStyle: { width: 1.5, type: 'dashed', color: '#F6B04B' },
      itemStyle: { color: '#F6B04B' },
    },
  ],
}))
</script>

<template>
  <VChart :option="option" autoresize :style="{ height: `${height || 390}px` }" />
</template>
