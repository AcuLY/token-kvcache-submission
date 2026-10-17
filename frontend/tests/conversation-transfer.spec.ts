import { mount } from '@vue/test-utils'
import { expect, it } from 'vitest'
import ConversationPanel from '@/components/ConversationPanel.vue'

it('labels the sending message by agent and turn with distinct icons, and leaves the final reply unsent', async () => {
  const wrapper = mount(ConversationPanel, { props: {
    messages: [1, 2, 3, 4].map(turn => ({ agent_id: turn % 2 ? 'a' : 'b', turn_index: turn, text: 'Reply' })),
    transfers: [
      { transfer_id: 'token', source_agent_id: 'a', turn_id: 1, mode: 'TOKEN', variant: 'FULL', status: 'COMMITTED' },
      { transfer_id: 'full', source_agent_id: 'b', turn_id: 2, mode: 'KV', variant: 'FULL', status: 'COMMITTED' },
      { transfer_id: 'delta', source_agent_id: 'a', turn_id: 3, mode: 'KV', variant: 'DELTA', status: 'FAILED' },
      { transfer_id: 'wrong-agent', source_agent_id: 'a', turn_id: 4, mode: 'TOKEN', variant: 'FULL' },
    ],
  } })
  const badges = wrapper.findAll('.transfer-badge')
  expect(badges.map(badge => badge.text())).toEqual(['TOKEN / FULL', 'KV / FULL', 'KV / DELTA'])
  expect(badges.map(badge => badge.find('svg').attributes('class'))).toEqual(expect.arrayContaining([
    expect.stringContaining('file-text'), expect.stringContaining('layers'), expect.stringContaining('git-branch'),
  ]))
  expect(wrapper.findAll('article')[2]!.text()).toContain('交接失败')
  expect(wrapper.findAll('article')[3]!.text()).toContain('本轮无交接')
  await badges[1]!.trigger('click')
  expect(wrapper.emitted('selectTransfer')).toEqual([['full']])
  wrapper.unmount()
})
