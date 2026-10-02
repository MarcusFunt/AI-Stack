import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createRealtimeVoiceClient, dispatchVoiceControlMessage, readSelectedIceCandidatePath } from './realtimeVoice'

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
    onIcePathChanged: vi.fn(),
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

  afterEach(() => {
    vi.useRealTimers()
  })

  it('stops ICE monitoring on client error and discards pending stats', async () => {
    vi.useFakeTimers()
    let resolveStats: ((value: Map<string, Record<string, unknown>>) => void) | undefined
    const getStats = vi.fn(() => new Promise((resolve) => { resolveStats = resolve }))
    let transportCallbacks: Record<string, (...args: unknown[]) => void> = {}
    mocks.SmallWebRTCTransport.mockImplementation(function (options) {
      return { ...options, pc: { getStats } }
    })
    mocks.PipecatClient.mockImplementation(function (options) {
      transportCallbacks = options.callbacks
      return { connect: mocks.clientConnect, disconnect: mocks.clientDisconnect }
    })
    const onIcePathChanged = vi.fn()
    const onTransportStateChanged = vi.fn()
    const client = createRealtimeVoiceClient(
      { getAudioTracks: () => [{ enabled: true }] } as unknown as MediaStream,
      { ...callbacks, onIcePathChanged, onTransportStateChanged },
    )

    transportCallbacks.onTransportStateChanged('connected')
    expect(getStats).toHaveBeenCalledOnce()
    transportCallbacks.onError(new Error('transport lost'))
    resolveStats?.(new Map([
      ['pair', { type: 'candidate-pair', selected: true, localCandidateId: 'local', remoteCandidateId: 'remote' }],
      ['local', { type: 'local-candidate', candidateType: 'relay', protocol: 'udp' }],
      ['remote', { type: 'remote-candidate', candidateType: 'host', protocol: 'udp' }],
    ]))
    await vi.advanceTimersByTimeAsync(3_000)
    const statsCalls = getStats.mock.calls.length
    const publishedPaths = [...onIcePathChanged.mock.calls]
    await client.disconnect()

    expect(onTransportStateChanged).toHaveBeenLastCalledWith('error')
    expect(statsCalls).toBe(1)
    expect(publishedPaths).toEqual([[null]])
  })

  it('reports the selected local and remote ICE candidate types', async () => {
    const reports = new Map([
      ['transport-1', { type: 'transport', selectedCandidatePairId: 'pair-1' }],
      ['pair-1', {
        type: 'candidate-pair',
        localCandidateId: 'local-1',
        remoteCandidateId: 'remote-1',
      }],
      ['local-1', { type: 'local-candidate', candidateType: 'relay', protocol: 'udp', relayProtocol: 'tls' }],
      ['remote-1', { type: 'remote-candidate', candidateType: 'host', protocol: 'udp' }],
    ])
    const peer = { getStats: vi.fn().mockResolvedValue(reports) } as unknown as RTCPeerConnection

    await expect(readSelectedIceCandidatePath(peer)).resolves.toEqual({
      localType: 'relay',
      localProtocol: 'udp',
      localRelayProtocol: 'tls',
      remoteType: 'host',
      remoteProtocol: 'udp',
    })
  })

  it('does not treat an unselected nominated candidate pair as the active path', async () => {
    const reports = new Map([
      ['pair-1', {
        type: 'candidate-pair',
        state: 'succeeded',
        nominated: true,
        localCandidateId: 'local-1',
        remoteCandidateId: 'remote-1',
      }],
      ['local-1', { type: 'local-candidate', candidateType: 'relay', protocol: 'udp' }],
      ['remote-1', { type: 'remote-candidate', candidateType: 'host', protocol: 'udp' }],
    ])
    const peer = { getStats: vi.fn().mockResolvedValue(reports) } as unknown as RTCPeerConnection

    await expect(readSelectedIceCandidatePath(peer)).resolves.toBeNull()
  })

  it('publishes the selected candidate path while connected and clears it on disconnect', async () => {
    const expected = {
      localType: 'relay',
      localProtocol: 'udp',
      localRelayProtocol: 'tls',
      remoteType: 'host',
      remoteProtocol: 'udp',
    }
    const reports = new Map([
      ['transport-1', { type: 'transport', selectedCandidatePairId: 'pair-1' }],
      ['pair-1', { type: 'candidate-pair', localCandidateId: 'local-1', remoteCandidateId: 'remote-1' }],
      ['local-1', { type: 'local-candidate', candidateType: 'relay', protocol: 'udp', relayProtocol: 'tls' }],
      ['remote-1', { type: 'remote-candidate', candidateType: 'host', protocol: 'udp' }],
    ])
    let transportCallbacks: Record<string, (state?: string) => void> = {}
    mocks.SmallWebRTCTransport.mockImplementation(function (options) {
      return { ...options, pc: { getStats: vi.fn().mockResolvedValue(reports) } }
    })
    mocks.PipecatClient.mockImplementation(function (options) {
      transportCallbacks = options.callbacks
      return { connect: mocks.clientConnect, disconnect: mocks.clientDisconnect }
    })
    const onIcePathChanged = vi.fn()
    const client = createRealtimeVoiceClient(
      { getAudioTracks: () => [{ enabled: true }] } as unknown as MediaStream,
      { ...callbacks, onIcePathChanged },
    )

    transportCallbacks.onTransportStateChanged('connected')
    await vi.waitFor(() => expect(onIcePathChanged).toHaveBeenCalledWith(expected))
    await client.disconnect()

    expect(onIcePathChanged).toHaveBeenLastCalledWith(null)
  })

  it('discards an in-flight stats response after transport disconnects', async () => {
    const reports = new Map([
      ['transport-1', { type: 'transport', selectedCandidatePairId: 'pair-1' }],
      ['pair-1', { type: 'candidate-pair', localCandidateId: 'local-1', remoteCandidateId: 'remote-1' }],
      ['local-1', { type: 'local-candidate', candidateType: 'relay', protocol: 'udp', relayProtocol: 'tls' }],
      ['remote-1', { type: 'remote-candidate', candidateType: 'host', protocol: 'udp' }],
    ])
    let resolveStats: ((value: Map<string, Record<string, unknown>>) => void) | undefined
    let transportCallbacks: Record<string, (state?: string) => void> = {}
    mocks.SmallWebRTCTransport.mockImplementation(function (options) {
      return {
        ...options,
        pc: { getStats: vi.fn(() => new Promise((resolve) => { resolveStats = resolve })) },
      }
    })
    mocks.PipecatClient.mockImplementation(function (options) {
      transportCallbacks = options.callbacks
      return { connect: mocks.clientConnect, disconnect: mocks.clientDisconnect }
    })
    const onIcePathChanged = vi.fn()
    createRealtimeVoiceClient(
      { getAudioTracks: () => [{ enabled: true }] } as unknown as MediaStream,
      { ...callbacks, onIcePathChanged },
    )

    transportCallbacks.onTransportStateChanged('connected')
    transportCallbacks.onTransportStateChanged('disconnected')
    resolveStats?.(reports)
    await vi.waitFor(() => expect(onIcePathChanged).toHaveBeenCalledWith(null))
    await Promise.resolve()

    expect(onIcePathChanged).toHaveBeenLastCalledWith(null)
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

  it('mutes and unmutes the already-captured microphone track', () => {
    const track = { enabled: true } as MediaStreamTrack
    const stream = { getAudioTracks: () => [track] } as unknown as MediaStream
    const client = createRealtimeVoiceClient(stream, callbacks)
    const options = mocks.SmallWebRTCTransport.mock.calls[0][0] as {
      mediaManager: { isMicEnabled: boolean }
    }

    client.setMicrophoneEnabled(false)
    expect(track.enabled).toBe(false)
    expect(options.mediaManager.isMicEnabled).toBe(false)

    client.setMicrophoneEnabled(true)
    expect(track.enabled).toBe(true)
    expect(options.mediaManager.isMicEnabled).toBe(true)
  })

  it('posts offers to the same-origin proxy and keeps the ticket in a header', async () => {
    const stream = { getAudioTracks: () => [{ enabled: true }] } as unknown as MediaStream
    const client = createRealtimeVoiceClient(stream, callbacks)
    await client.connect({
      offerUrl: '/api/v1/realtime/sessions/session-1/offer',
      ticket: 'one-use-ticket',
      iceServers: [{ urls: 'turns:host.tailnet.example:8447?transport=tcp' }],
    })

    expect(mocks.clientConnect).toHaveBeenCalledOnce()
    const request = mocks.clientConnect.mock.calls[0][0]
    expect(request).toEqual({
      webrtcRequestParams: expect.objectContaining({ endpoint: '/api/v1/realtime/sessions/session-1/offer' }),
      iceConfig: { iceServers: [{ urls: 'turns:host.tailnet.example:8447?transport=tcp' }] },
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
