import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import ResumeLifecycleBadge from './ResumeLifecycleBadge.vue'

describe('ResumeLifecycleBadge', () => {
  it.each([
    [{ activated_at: '2026-01-01', expires_at: '2999-01-01' }, 'active', '在线'],
    [{ activated_at: null, expires_at: null, candidate_expires_at: '2999-01-01' }, 'candidate', '候选'],
    [{ activated_at: null, expires_at: null, candidate_expires_at: null }, 'legacy', '兼容'],
  ])('renders lifecycle without requiring non-null times', (resume, state, label) => {
    const wrapper = mount(ResumeLifecycleBadge, { props: { resume } })
    expect(wrapper.attributes('data-state')).toBe(state)
    expect(wrapper.text()).toContain(label)
  })

  it('shows a stable conflict reason', () => {
    const wrapper = mount(ResumeLifecycleBadge, { props: { resume: {
      activated_at: null, candidate_expires_at: '2999-01-01',
      replacement_conflict_reason: 'replacement_conflict',
    } } })
    expect(wrapper.text()).toContain('replacement_conflict')
  })

  it('uses the shared UTC-naive resolver at an Asia/Shanghai boundary', () => {
    const wrapper = mount(ResumeLifecycleBadge, { props: {
      resume: {
        activated_at: null,
        candidate_expires_at: '2026-08-20 16:00:00.000000',
      },
      now: '2026-08-21T00:00:00+08:00',
    } })
    expect(wrapper.attributes('data-state')).toBe('history')
  })
})
