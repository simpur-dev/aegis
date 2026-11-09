import { afterEach, describe, expect, it, vi } from 'vitest'

import { assistantApi } from '@/api/assistant'
import { clearOperatorSeat, setOperatorSeat } from '@/api/operatorSeat'

import { resetAssistantCapabilities, useAssistantCapabilities } from './useAssistantCapabilities'

afterEach(async () => {
  clearOperatorSeat()
  await Promise.resolve()
  vi.restoreAllMocks()
  resetAssistantCapabilities()
})

describe('能力面跟随席位', () => {
  it('换席位后重新读一份，按钮的 allowed 不会停在上一个角色', async () => {
    const spy = vi.spyOn(assistantApi, 'capabilities').mockResolvedValue({
      llm_configured: false,
      actions: [],
      session_ttl_seconds: 1800,
      confirm_ttl_seconds: 300,
      control_actions_persistent: true,
    })

    setOperatorSeat('duty-01')
    await vi.waitFor(() => expect(spy).toHaveBeenCalledTimes(1))

    setOperatorSeat('lead-01')
    await vi.waitFor(() => expect(spy).toHaveBeenCalledTimes(2))

    expect(useAssistantCapabilities().capabilities.value).not.toBeNull()
  })
})
