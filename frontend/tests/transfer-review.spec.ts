import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { expect, it } from 'vitest'
import ConversationView from '@/views/ConversationView.vue'
import CurrentHandoffPanel from '@/components/CurrentHandoffPanel.vue'
import { useAppStore } from '@/stores/app'

it('follows latest, pins a historical transfer across updates, and resets for another session', async () => {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useAppStore()
  store.sessions = [{
    id: 'one', status: 'completed', transfers: [
      { transfer_id: 't1', mode: 'TOKEN', variant: 'FULL', timings: { p2p_ms: 11 } },
      { transfer_id: 't2', mode: 'KV', variant: 'DELTA', timings: { p2p_ms: 22 } },
    ],
  }, { id: 'two', status: 'completed', transfers: [{ transfer_id: 'other' }] }]
  store.selectedSessionId = 'one'
  const wrapper = mount(ConversationView, {
    global: {
      plugins: [pinia],
      stubs: {
        RouterLink: true, SessionSidebar: true, ConversationPanel: true,
        NetworkChart: true, ObservationEventList: true,
        NButton: { template: '<button><slot /></button>' }, NIcon: true,
      },
    },
  })
  const inspected = () => wrapper.findComponent(CurrentHandoffPanel).props('transfer')?.transfer_id
  expect(inspected()).toBe('t2')
  await wrapper.find('.transfer-history button').trigger('click')
  expect(inspected()).toBe('t1')
  store.sessions[0]!.transfers!.push({ transfer_id: 't3', mode: 'KV', variant: 'FULL' })
  await nextTick()
  expect(inspected()).toBe('t1')
  expect(wrapper.find('.phase-panel').text()).toContain('第 1 次交接')
  await wrapper.find('.review-toolbar button').trigger('click')
  expect(inspected()).toBe('t3')
  await wrapper.find('.transfer-history button').trigger('click')
  store.selectedSessionId = 'two'
  await nextTick()
  expect(inspected()).toBe('other')
  expect(wrapper.find('.review-toolbar').text()).toContain('跟随最新')
  wrapper.unmount()
})
