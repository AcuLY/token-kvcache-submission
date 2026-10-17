import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import EmptyState from '@/components/EmptyState.vue'

describe('EmptyState', () => {
  it('communicates unsupported capabilities without fabricated values', () => {
    const wrapper = mount(EmptyState, {
      props: {
        title: '当前环境未提供 Cache / KV 能力',
        description: '首轮仅实现 TOKEN / FULL。',
        kind: 'unavailable',
      },
    })

    expect(wrapper.attributes('role')).toBe('status')
    expect(wrapper.text()).toContain('未提供 Cache / KV 能力')
    expect(wrapper.text()).not.toContain('0 MB')
  })
})
