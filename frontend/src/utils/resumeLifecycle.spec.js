import { describe, expect, it } from 'vitest'
import { parseUtcTimestamp, resolveResumeLifecycle } from './resumeLifecycle'

describe('resume lifecycle UTC boundary', () => {
  const shanghaiMidnight = '2026-08-21T00:00:00+08:00'

  it('parses UTC-naive DATETIME(6) independently of browser timezone', () => {
    expect(parseUtcTimestamp('2026-08-20 16:00:00.123456').toISOString())
      .toBe('2026-08-20T16:00:00.123Z')
  })

  it('expires an active resume at the Asia/Shanghai boundary', () => {
    expect(resolveResumeLifecycle({
      activated_at: '2026-08-01 00:00:00.000000',
      expires_at: '2026-08-20 16:00:00.000000',
    }, shanghaiMidnight)).toBe('history')
  })

  it('uses candidate_expires_at for candidates and moves expired candidates to history', () => {
    const candidate = { activated_at: null, expires_at: null }
    expect(resolveResumeLifecycle({
      ...candidate, candidate_expires_at: '2026-08-20 16:00:00.000000',
    }, shanghaiMidnight)).toBe('history')
    expect(resolveResumeLifecycle({
      ...candidate, candidate_expires_at: '2026-08-20 16:00:00.001000',
    }, shanghaiMidnight)).toBe('candidate')
  })

  it('prioritizes deletion and delisting over otherwise active timestamps', () => {
    const active = {
      activated_at: '2026-08-01 00:00:00', expires_at: '2026-09-01 00:00:00',
    }
    expect(resolveResumeLifecycle({ ...active, deleted_at: '2026-08-02' }, shanghaiMidnight))
      .toBe('history')
    expect(resolveResumeLifecycle({ ...active, delist_reason: 'manual_delist' }, shanghaiMidnight))
      .toBe('history')
  })
})
