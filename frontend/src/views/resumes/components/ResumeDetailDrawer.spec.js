import { flushPromises, shallowMount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ResumeDetailDrawer from './ResumeDetailDrawer.vue'
import { fetchResumeDetail } from '@/api/resumes'

vi.mock('@/api/resumes', () => ({
  fetchResumeDetail: vi.fn(),
  updateResume: vi.fn(),
  delistResume: vi.fn(),
  extendResume: vi.fn(),
}))

describe('ResumeDetailDrawer lifecycle mutation gate', () => {
  beforeEach(() => vi.clearAllMocks())

  const uiStubs = {
    ElDescriptionsItem: true, ElOption: true, ElSelect: true,
    ElInputNumber: true, ElTag: true, ElDescriptions: true, ElButton: true,
    ElIcon: true, ElDropdownItem: true, ElDropdownMenu: true, ElDropdown: true,
  }

  it.each([
    ['candidate', { activated_at: null, expires_at: null, candidate_expires_at: '2999-01-01' }],
    ['history', { activated_at: '2026-01-01', expires_at: '2026-01-02', delist_reason: 'expired' }],
  ])('disables edit, extend, and delist for %s resumes', async (_label, lifecycle) => {
    fetchResumeDetail.mockResolvedValue({ id: 1, version: 1, ...lifecycle })
    const wrapper = shallowMount(ResumeDetailDrawer, {
      props: { modelValue: true, resumeId: 1 },
      global: { stubs: uiStubs },
    })
    await flushPromises()
    expect(wrapper.vm.canMutate).toBe(false)
  })

  it('keeps lifecycle mutations enabled for an online resume', async () => {
    fetchResumeDetail.mockResolvedValue({
      id: 2, version: 1, activated_at: '2026-01-01', expires_at: '2999-01-01',
      deleted_at: null, delist_reason: null,
    })
    const wrapper = shallowMount(ResumeDetailDrawer, {
      props: { modelValue: true, resumeId: 2 },
      global: { stubs: uiStubs },
    })
    await flushPromises()
    expect(wrapper.vm.canMutate).toBe(true)
  })
})
