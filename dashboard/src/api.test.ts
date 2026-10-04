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

  it('sends the selected Qwen speaker, expressive instructions, and English language to the gateway', async () => {
    const audio = new Blob(['audio'], { type: 'audio/wav' })
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      blob: vi.fn().mockResolvedValue(audio),
    })
    vi.stubGlobal('fetch', fetchMock)

    await localAI.speak('Say hello.', 'Ryan', 'Cheerfully, with a dramatic pause.')

    expect(fetchMock).toHaveBeenCalledWith('/api/v1/audio/speech', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{"model":"local-tts","input":"Say hello.","voice":"Ryan","language":"English","instruct":"Cheerfully, with a dramatic pause.","response_format":"wav"}',
    })
  })

  it('forwards Whisper transcription controls and timestamp granularity', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue({ text: 'Hello.' }),
    })
    vi.stubGlobal('fetch', fetchMock)

    await localAI.transcribe(new File(['audio'], 'sample.wav', { type: 'audio/wav' }), {
      language: 'en',
      prompt: 'Names: Alex and Morgan.',
      temperature: 0.2,
      timestamp_granularities: ['segment', 'word'],
      response_format: 'verbose_json',
    })

    const body = fetchMock.mock.calls[0][1].body as FormData
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/audio/transcriptions')
    expect(body.get('language')).toBe('en')
    expect(body.get('prompt')).toBe('Names: Alex and Morgan.')
    expect(body.get('temperature')).toBe('0.2')
    expect(body.getAll('timestamp_granularities[]')).toEqual(['segment', 'word'])
    expect(body.get('response_format')).toBe('verbose_json')
  })
})
