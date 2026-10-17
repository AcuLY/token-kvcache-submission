<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink, RouterView, useRoute } from 'vue-router'
import {
  Activity,
  BarChart3,
  Boxes,
  CircleHelp,
  FlaskConical,
  Menu,
  MessageSquare,
  Settings2,
  Users,
  X,
} from 'lucide-vue-next'
import { NAlert, NButton, NDrawer, NDrawerContent, NIcon, NSpin, NTooltip } from 'naive-ui'
import BrandMark from './BrandMark.vue'
import StatusDot from './StatusDot.vue'
import { useAppStore } from '@/stores/app'

const store = useAppStore()
const route = useRoute()
const mobileOpen = ref(false)

const baseNavigation = [
  { label: '会话', to: '/sessions', icon: MessageSquare },
  { label: 'Agent 与配置', to: '/agents', icon: Users },
  { label: '实验分析', to: '/experiments', icon: BarChart3 },
  { label: '环境与适配器', to: '/environment', icon: Boxes },
]

const extensionNavigation = computed(() =>
  store.uiExtensions.map((extension) => ({
    label: extension.label,
    to: extension.route,
    icon: extension.kind === 'mock_lab' ? FlaskConical : Settings2,
  })),
)

const connection = computed(() => {
  if (store.error) return { label: '中心不可用', tone: 'danger' as const }
  if (store.realtimeStatus === 'connected') return { label: '中心已连接', tone: 'success' as const }
  if (store.realtimeStatus === 'reconnecting') return { label: '正在重连', tone: 'warning' as const }
  return { label: '正在连接', tone: 'primary' as const }
})

function active(path: string): boolean {
  return route.path === path || (path !== '/sessions' && route.path.startsWith(path))
}
</script>

<template>
  <div class="app-shell">
    <header class="app-header">
      <div class="app-header__inner">
        <RouterLink class="brand" to="/sessions" aria-label="前往会话页">
          <BrandMark />
          <span>Token/KV Cache 通信实验台</span>
        </RouterLink>

        <nav class="desktop-nav" aria-label="主导航">
          <RouterLink
            v-for="item in baseNavigation"
            :key="item.to"
            :to="item.to"
            class="nav-link"
            :class="{ 'nav-link--active': active(item.to) }"
          >
            <component :is="item.icon" :size="18" :stroke-width="1.8" />
            <span>{{ item.label }}</span>
          </RouterLink>
          <RouterLink
            v-for="item in extensionNavigation"
            :key="item.to"
            :to="item.to"
            class="nav-link nav-link--extension"
            :class="{ 'nav-link--active': active(item.to) }"
          >
            <component :is="item.icon" :size="18" :stroke-width="1.8" />
            <span>{{ item.label }}</span>
          </RouterLink>
        </nav>

        <div class="header-actions">
          <span class="health-status">
            <StatusDot :tone="connection.tone" :pulse="connection.tone === 'primary'" />
            {{ connection.label }}
          </span>
          <span v-if="store.environment" class="environment-name truncate">
            环境：{{ store.environment.name }}
          </span>
          <NTooltip trigger="hover">
            <template #trigger>
              <NButton quaternary circle aria-label="系统说明">
                <template #icon><NIcon><CircleHelp /></NIcon></template>
              </NButton>
            </template>
            Coordinator 仅负责控制与观测；Token payload 在 Agent 间点对点传输。
          </NTooltip>
          <NButton class="mobile-menu" quaternary circle aria-label="打开导航" @click="mobileOpen = true">
            <template #icon><NIcon><Menu /></NIcon></template>
          </NButton>
        </div>
      </div>
    </header>

    <div v-if="store.error" class="global-alert">
      <NAlert type="error" :bordered="false" closable @close="store.error = null">
        <span class="alert-content">{{ store.error }}<NButton size="small" secondary type="error" @click="store.loadBootstrap()">重新加载</NButton></span>
      </NAlert>
    </div>

    <div v-if="store.actionError" class="action-alert">
      <NAlert type="error" closable @close="store.clearActionError()">{{ store.actionError }}</NAlert>
    </div>

    <main class="app-main">
      <div v-if="store.loading" class="app-loading">
        <NSpin size="large" />
        <span>正在载入实验台…</span>
      </div>
      <RouterView v-else />
    </main>

    <NDrawer v-model:show="mobileOpen" placement="right" :width="290">
      <NDrawerContent :native-scrollbar="false">
        <template #header>
          <div class="drawer-header">
            <div class="drawer-brand"><BrandMark />实验台导航</div>
            <NButton quaternary circle aria-label="关闭导航" @click="mobileOpen = false">
              <template #icon><NIcon><X /></NIcon></template>
            </NButton>
          </div>
        </template>
        <nav class="drawer-nav" aria-label="移动端主导航">
          <RouterLink
            v-for="item in [...baseNavigation, ...extensionNavigation]"
            :key="item.to"
            :to="item.to"
            class="drawer-link"
            :class="{ 'drawer-link--active': active(item.to) }"
            @click="mobileOpen = false"
          >
            <component :is="item.icon" :size="20" />
            {{ item.label }}
          </RouterLink>
        </nav>
        <div class="drawer-status">
          <Activity :size="18" />
          <span>{{ connection.label }}</span>
          <span v-if="store.environment" class="muted">{{ store.environment.name }}</span>
        </div>
      </NDrawerContent>
    </NDrawer>
  </div>
</template>

<style scoped>
.app-shell {
  min-height: 100vh;
}

.app-header {
  position: sticky;
  z-index: 30;
  top: 0;
  height: var(--header-height);
  background: rgb(255 255 255 / 96%);
  border-bottom: 1px solid var(--color-border);
  backdrop-filter: blur(14px);
}

.app-header__inner {
  display: grid;
  width: min(100%, var(--content-max));
  height: 100%;
  padding: 0 24px;
  margin: 0 auto;
  align-items: center;
  grid-template-columns: minmax(260px, 1fr) auto minmax(260px, 1fr);
}

.brand {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  color: var(--color-text);
  font-size: 19px;
  font-weight: 700;
  letter-spacing: -0.02em;
  white-space: nowrap;
}

.desktop-nav {
  display: flex;
  height: 100%;
  align-items: stretch;
  justify-content: center;
  gap: 6px;
}

.nav-link {
  position: relative;
  display: inline-flex;
  min-width: 94px;
  padding: 0 15px;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: #283550;
  font-size: 15px;
  font-weight: 500;
  transition: color 160ms ease, background 160ms ease;
}

.nav-link:hover {
  color: var(--color-primary);
  background: #f8faff;
}

.nav-link::after {
  position: absolute;
  right: 10px;
  bottom: -1px;
  left: 10px;
  height: 3px;
  border-radius: 2px 2px 0 0;
  background: transparent;
  content: '';
}

.nav-link--active {
  color: var(--color-primary);
  font-weight: 650;
}

.nav-link--active::after {
  background: var(--color-primary);
}

.nav-link--extension {
  min-width: 110px;
}

.header-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 14px;
}

.health-status {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--color-text-secondary);
  font-size: 13px;
  white-space: nowrap;
}

.environment-name {
  max-width: 180px;
  padding: 7px 10px;
  color: var(--color-text-secondary);
  font-size: 13px;
  background: var(--color-surface-subtle);
  border: 1px solid var(--color-border-soft);
  border-radius: var(--radius-sm);
}

.mobile-menu { display: none; }

.global-alert {
  position: relative;
  z-index: 20;
  max-width: var(--content-max);
  margin: 10px auto 0;
  padding: 0 24px;
}

.action-alert {
  position: fixed;
  z-index: 50;
  top: 76px;
  right: 20px;
  width: min(420px, calc(100vw - 32px));
  box-shadow: 0 12px 32px rgb(12 38 80 / 14%);
}

.app-main {
  min-height: calc(100vh - var(--header-height));
}

.app-loading {
  display: flex;
  min-height: calc(100vh - var(--header-height));
  align-items: center;
  justify-content: center;
  flex-direction: column;
  gap: 14px;
  color: var(--color-text-secondary);
  font-size: 14px;
}

.drawer-brand {
  display: flex;
  align-items: center;
  gap: 10px;
  font-weight: 700;
}

.drawer-header,
.alert-content {
  display: flex;
  width: 100%;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.drawer-nav {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.drawer-link {
  display: flex;
  min-height: 48px;
  padding: 0 14px;
  align-items: center;
  gap: 12px;
  border-radius: 7px;
  color: var(--color-text-secondary);
  font-size: 15px;
}

.drawer-link--active {
  color: var(--color-primary);
  background: var(--color-primary-soft);
  font-weight: 650;
}

.drawer-status {
  display: grid;
  margin-top: 24px;
  padding: 14px;
  grid-template-columns: auto 1fr;
  gap: 5px 10px;
  background: var(--color-surface-subtle);
  border: 1px solid var(--color-border-soft);
  border-radius: var(--radius-md);
  font-size: 13px;
}

.drawer-status .muted { grid-column: 2; }

@media (max-width: 1260px) {
  .app-header__inner {
    grid-template-columns: auto 1fr auto;
  }
  .brand span { display: none; }
  .nav-link { min-width: auto; padding-inline: 12px; }
  .health-status { display: none; }
}

@media (max-width: 900px) {
  .desktop-nav,
  .environment-name { display: none; }
  .app-header__inner {
    grid-template-columns: 1fr auto;
    padding: 0 16px;
  }
  .brand span { display: inline; font-size: 17px; }
  .mobile-menu { display: inline-flex; }
}

@media (max-width: 520px) {
  .brand span { font-size: 15px; }
  .brand-mark { width: 27px; height: 27px; }
  .app-header__inner { padding: 0 12px; }
  .header-actions { gap: 2px; }
}
</style>
