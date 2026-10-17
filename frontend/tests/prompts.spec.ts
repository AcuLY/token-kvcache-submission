import { describe, expect, it } from 'vitest'
import { mergeProfilePrompt } from '@/utils/prompts'

describe('legacy prompt migration', () => {
  it('preserves old instructions visibly and never appends them again after editing', () => {
    const original = {
      agent_id: 'agent-a', display_name: 'Agent A', system_prompt: '原提示词',
      role_description: '旧角色', local_context: '旧背景', generation_config: {},
    }
    const merged = mergeProfilePrompt(original)
    expect(merged.system_prompt).toBe('原提示词\n\n角色要求：旧角色\n\n背景：旧背景')
    expect(merged.role_description).toBe('')
    expect(merged.local_context).toBe('')
    expect(mergeProfilePrompt(merged)).toEqual(merged)
    expect(mergeProfilePrompt({ ...merged, system_prompt: '全新辩论要求' }).system_prompt)
      .toBe('全新辩论要求')
    expect(original.local_context).toBe('旧背景')
  })
})
