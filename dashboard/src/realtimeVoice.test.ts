import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createRealtimeVoiceClient, dispatchVoiceControlMessage } from './realtimeVoice'

const mocks = vi.hoisted(() => ({
  clientConnect: vi.fn(),
  clientDisconnect: vi.fn(),
  PipecatClient: vi.fn(),
  SmallWebRTCTransport: vi.fn(),
}))

vi.mock('@pipecat-ai/client-js', () => ({
  PipecatClient: mocks.PipecatClient,
}))

vi.mock('@pipecat-ai/small-webrtc-transport', () => ({
  SmallWebRTCTransport: mocks.SmallWebRTCTransport,
}))

describe('createRealtimeVoiceClient', () => {
  const callbacks = {
    onTransportStateChanged: vi.fn(),
    onDisconnected: vi.fn(),
    onServerMessage: vi.fn(),
    onRemoteStream: vi.fn(),
  }

  beforeEach(() => {
    mocks.clientConnect.mockReset().mockResolvedValue(undefined)
    mocks.clientDisconnect.mockReset().mockResolvedValue(undefined)
    mocks.PipecatClient.mockReset().mockImplementation(function () { return {
      connect: mocks.clientConnect,
      disconnect: mocks.clientDisconnect,
    } })
    mocks.SmallWebRTCTransport.mockReset().mockImplementation(function (options) { return options })
  })

  it('uses the already-captured microphone track without requesting another one', async () => {
    const track = { enabled: true } as MediaStreamTrack
    const stream = { getAudioTracks: () => [track] } as unknown as MediaStream
    const getUserMedia = vi.fn()
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia },
    })

    createRealtimeVoiceClient(stream, callbacks)
    const options = mocks.SmallWebRTCTransport.mock.calls[0][0] as {
      mediaManager: { tracks: () => { local: { audio?: MediaStreamTrack } } }
    }
    await options.mediaManager.tracks()

    expect(options.mediaManager.tracks().local.audio).toBe(track)
    expect(getUserMedia).not.toHaveBeenCalled()
  })

  it('posts offers to the same-origin proxy and keeps the ticket in a header', async () => {
    const stream = { getAudioTracks: () => [{ enabled: true }] } as unknown as MediaStream
    const client = createRealtimeVoiceClient(stream, callbacks)
    await client.connect({
      offerUrl: '/api/v1/realtime/sessions/session-1/offer',
      ticket: 'one-use-ticket',
      iceServers: [{ urls: 'turns:host.tailnet.example:8446?transport=tcp' }],
    })

    expect(mocks.clientConnect).toHaveBeenCalledOnce()
    const request = mocks.clientConnect.mock.calls[0][0]
    expect(request).toEqual({
      webrtcRequestParams: expect.objectContaining({ endpoint: '/api/v1/realtime/sessions/session-1/offer' }),
      iceConfig: { iceServers: [{ urls: 'turns:host.tailnet.example:8446?transport=tcp' }] },
    })
    expect(request.webrtcRequestParams.headers).toBeInstanceOf(Headers)
    expect(request.webrtcRequestParams.headers.get('X-Voice-Session-Ticket')).toBe('one-use-ticket')
    expect(request.webrtcRequestParams.endpoint).not.toContain('one-use-ticket')
  })

  it('delivers the AI-Stack control envelope intact while retaining Pipecat signaling messages', () => {
    const controlEvents: unknown[] = []
    const pipecatMessages: string[] = []
    const message = JSON.stringify({
      protocol: 'ai-stack.voice.v1',
      type: 'response.output_text.delta',
      generation_id: 7,
      delta: 'Hello',
    })

    const handled = dispatchVoiceControlMessage(
      message,
      (event) => controlEvents.push(event),
      (fallback) => pipecatMessages.push(fallback),
    )

    expect(handled).toBe(true)
    expect(controlEvents).toEqual([JSON.parse(message)])
    expect(pipecatMessages).toEqual([])
    const signalling = JSON.stringify({ type: 'signalling', message: { type: 'peer-left' } })
    expect(dispatchVoiceControlMessage(signalling, vi.fn(), (fallback) => pipecatMessages.push(fallback))).toBe(false)
    expect(pipecatMessages).toEqual([signalling])
  })
})
