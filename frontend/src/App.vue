<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, watch } from 'vue'
import {
  NConfigProvider,
  NDialogProvider,
  NLoadingBarProvider,
  NMessageProvider,
  NNotificationProvider,
  zhCN,
  dateZhCN,
  type GlobalThemeOverrides,
} from 'naive-ui'
import AppShell from '@/components/AppShell.vue'
import { useAppStore } from '@/stores/app'
import { installExtensionRoutes, router } from '@/router'

const store = useAppStore()

const themeOverrides = computed<GlobalThemeOverrides>(() => ({
  common: {
    primaryColor: '#145DFF',
    primaryColorHover: '#3678FF',
    primaryColorPressed: '#0C47D9',
    primaryColorSuppl: '#145DFF',
    infoColor: '#145DFF',
    successColor: '#16A34A',
    warningColor: '#F59E0B',
    errorColor: '#E5484D',
    textColorBase: '#0B1739',
    borderRadius: '6px',
    fontFamily: 'Inter, "PingFang SC", "Microsoft YaHei", system-ui, sans-serif',
  },
  Button: {
    heightMedium: '40px',
    heightLarge: '44px',
    fontSizeMedium: '14px',
    borderRadiusMedium: '6px',
  },
  Input: {
    heightMedium: '40px',
    borderRadius: '5px',
  },
  Select: {
    peers: { InternalSelection: { heightMedium: '40px', borderRadius: '5px' } },
  },
  Tabs: {
    tabFontSizeMedium: '15px',
    tabTextColorActiveLine: '#145DFF',
    barColor: '#145DFF',
  },
  Card: {
    borderRadius: '8px',
    borderColor: '#DCE3EF',
  },
}))

watch(
  () => store.uiExtensions,
  (extensions) => installExtensionRoutes(router, extensions),
  { deep: true, immediate: true },
)

onMounted(async () => {
  await store.loadBootstrap()
  installExtensionRoutes(router, store.uiExtensions)
  store.startRealtime()
})

onBeforeUnmount(() => store.stopRealtime())
</script>

<template>
  <NConfigProvider :locale="zhCN" :date-locale="dateZhCN" :theme-overrides="themeOverrides">
    <NLoadingBarProvider>
      <NDialogProvider>
        <NNotificationProvider>
          <NMessageProvider>
            <AppShell />
          </NMessageProvider>
        </NNotificationProvider>
      </NDialogProvider>
    </NLoadingBarProvider>
  </NConfigProvider>
</template>
