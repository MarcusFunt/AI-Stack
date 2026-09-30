import { afterEach, describe, expect, it, vi } from 'vitest'
import { localAI } from './api'

describe('localAI realtime voice API', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('creates a session through the same-origin proxy and scopes offer signaling to it', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      id: 'session-1',
      offer_url: '/v1/realtime/sessions/session-1/offer',
      client_secret: { value: 'one-use-ticket', expires_at: 2_000 },
      ice_servers: [],
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)

    const result = await localAI.createRealtimeVoiceSession()

    expect(fetchMock).toHaveBeenCalledWith('/api/v1/realtime/sessions', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ language: 'en' }),
    }))
    expect(result.offer_url).toBe('/api/v1/realtime/sessions/session-1/offer')
  })
})
