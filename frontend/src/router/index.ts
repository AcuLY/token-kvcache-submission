import type { RouteRecordRaw, Router } from 'vue-router'
import { createRouter, createWebHistory } from 'vue-router'
import type { UiExtension } from '@/types/contracts'

const routes: RouteRecordRaw[] = [
  {
    path: '/',
    redirect: '/sessions',
  },
  {
    path: '/sessions',
    name: 'sessions',
    component: () => import('@/views/ConversationView.vue'),
    meta: { label: '会话' },
  },
  {
    path: '/agents',
    name: 'agents',
    component: () => import('@/views/AgentConfigView.vue'),
    meta: { label: 'Agent 与配置' },
  },
  {
    path: '/experiments',
    name: 'experiments',
    component: () => import('@/views/ExperimentView.vue'),
    meta: { label: '实验分析' },
  },
  {
    path: '/environment',
    name: 'environment',
    component: () => import('@/views/EnvironmentView.vue'),
    meta: { label: '环境与适配器' },
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('@/views/NotFoundView.vue'),
  },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

const installed = new Set<string>()

export function installExtensionRoutes(target: Router, extensions: UiExtension[]): void {
  let rematch = false
  const desired = new Set(extensions.map((extension) => `extension:${extension.id}`))
  for (const name of installed) {
    if (!desired.has(name)) {
      target.removeRoute(name)
      installed.delete(name)
      if (target.currentRoute.value.name === name) void target.replace('/environment')
    }
  }
  for (const extension of extensions) {
    const name = `extension:${extension.id}`
    if (installed.has(name)) continue
    const path = extension.route.startsWith('/') ? extension.route : `/${extension.route}`
    target.addRoute({
      path,
      name,
      component:
        extension.kind === 'mock_lab' || extension.id === 'mock-lab'
          ? () => import('@/views/MockLabView.vue')
          : () => import('@/views/ExtensionUnavailableView.vue'),
      props: { extension },
      meta: { extensionId: extension.id, label: extension.label },
    })
    installed.add(name)
    if (target.currentRoute.value.fullPath === path && target.currentRoute.value.name === 'not-found') {
      rematch = true
    }
  }
  void target.isReady().then(() => {
    const current = target.currentRoute.value
    if (rematch || target.resolve(current.fullPath).name !== current.name) {
      void target.replace(current.fullPath)
    }
  })
}
