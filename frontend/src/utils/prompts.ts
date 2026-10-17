import type { AgentProfile, SessionConfig } from '@/types/contracts'

// Keep legacy instructions visible in the single editor, then clear their
// original fields on save so they cannot survive as hidden model input.
export function mergeProfilePrompt(profile: AgentProfile): AgentProfile {
  const parts = [profile.system_prompt]
  if (profile.role_description?.trim()) parts.push(`角色要求：${profile.role_description}`)
  if (profile.local_context?.trim()) parts.push(`背景：${profile.local_context}`)
  return {
    ...profile,
    system_prompt: parts.filter((part) => part?.trim()).join('\n\n'),
    role_description: '',
    local_context: '',
  }
}

export function mergeConfigPrompts(config: SessionConfig): SessionConfig {
  return {
    ...config,
    agent_a_profile: mergeProfilePrompt(config.agent_a_profile),
    agent_b_profile: mergeProfilePrompt(config.agent_b_profile),
  }
}
