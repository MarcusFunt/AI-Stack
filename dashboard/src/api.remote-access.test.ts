import { afterEach, describe, expect, it, vi } from 'vitest'
import { localAI } from './api'

describe('localAI.remoteAccess', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('requests only the authenticated read-only Gateway status route', async () => {
    const status = { observed_at_utc: '2026-10-24T12:00:00Z' }
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => status })
    vi.stubGlobal('fetch', fetchMock)

    await expect(localAI.remoteAccess()).resolves.toEqual(status)
    expect(fetchMock).toHaveBeenCalledWith('/api/control/remote-access', expect.objectContaining({ method: 'GET' }))
  })
})
