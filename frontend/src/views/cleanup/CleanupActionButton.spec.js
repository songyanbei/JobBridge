import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import CleanupActionButton from './CleanupActionButton.vue'

describe('CleanupActionButton', () => {
  it('allows only a super admin in the required state', () => {
    const operator = mount(CleanupActionButton, { props: { role: 'operator', status: 'dead_letter', requiredStatus: 'dead_letter' } })
    const wrongState = mount(CleanupActionButton, { props: { role: 'super_admin', status: 'pending', requiredStatus: 'dead_letter' } })
    const allowed = mount(CleanupActionButton, { props: { role: 'super_admin', status: 'dead_letter', requiredStatus: 'dead_letter' } })
    expect(operator.element.disabled).toBe(true)
    expect(wrongState.element.disabled).toBe(true)
    expect(allowed.element.disabled).toBe(false)
  })

  it('prevents the approving admin from executing', () => {
    const wrapper = mount(CleanupActionButton, { props: {
      role: 'super_admin', status: 'approved', requiredStatus: 'approved',
      approvedBy: 'admin-a', username: 'admin-a', fourEyes: true,
    } })
    expect(wrapper.element.disabled).toBe(true)
  })

  it('allows a super admin to retry a media dead letter', () => {
    const wrapper = mount(CleanupActionButton, { props: {
      role: 'super_admin', status: 'dead_letter', requiredStatus: 'dead_letter',
    } })
    expect(wrapper.element.disabled).toBe(false)
  })
})
