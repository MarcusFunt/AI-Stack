import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import RealtimeVoicePanel from './RealtimeVoicePanel'

const mocks = vi.hoisted(() => ({
  createRealtimeVoiceSession: vi.fn(),
  createRealtimeVoiceClient: vi.fn(),
}))

vi.mock('./api', () => ({
  localAI: { createRealtimeVoiceSession: mocks.createRealtimeVoiceSession },
}))

vi.mock('@pipecat-ai/client-js', () => ({ PipecatClient: vi.fn() }))
vi.mock('@pipecat-ai/small-webrtc-transport', () => ({ SmallWebRTCTransport: vi.fn() }))

vi.mock('./realtimeVoice', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./realtimeVoice')>()
  return { ...actual, createRealtimeVoiceClient: mocks.createRealtimeVoiceClient }
})

const session = (id: string, ticket: string) => ({
  id,
  offer_url: `/api/v1/realtime/sessions/${id}/offer`,
  client_secret: { value: ticket, expires_at: 2_000 },
  ice_servers: [],
})

function setupMedia(settings: MediaTrackSettings = {}) {
  const track = {
    getSettings: vi.fn(() => settings),
    stop: vi.fn(),
  }
  const stream = {
    getTracks: () => [track],
    getAudioTracks: () => [track],
  } as unknown as MediaStream
  const getUserMedia = vi.fn().mockResolvedValue(stream)
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    value: { getUserMedia },
  })
  return { getUserMedia, stream, track }
}

function setupClient(connect: (params: unknown) => Promise<void> = async () => {}) {
  const client = {
    connect: vi.fn(connect),
    disconnect: vi.fn().mockResolvedValue(undefined),
    setMicrophoneEnabled: vi.fn(),
  }
  let callbacks: Record<string, (...args: never[]) => void> | undefined
  mocks.createRealtimeVoiceClient.mockImplementation((_stream, nextCallbacks) => {
    callbacks = nextCallbacks
    return client
  })
  return { client, callbacks: () => callbacks }
}

describe('RealtimeVoicePanel', () => {
  beforeEach(() => {
    mocks.createRealtimeVoiceSession.mockReset()
    mocks.createRealtimeVoiceClient.mockReset()
    mocks.createRealtimeVoiceSession.mockResolvedValue(session('session-1', 'ticket-one'))
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('requests microphone processing and reports the settings the browser applied', async () => {
    const media = setupMedia({ echoCancellation: true, noiseSuppression: false })
    setupClient()
    render(<RealtimeVoicePanel />)

    fireEvent.click(screen.getByRole('button', { name: 'Start a voice call' }))

    await waitFor(() => expect(mocks.createRealtimeVoiceSession).toHaveBeenCalledOnce())
    expect(media.getUserMedia).toHaveBeenCalledWith({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    })
    expect(screen.getByText('Echo cancellation').nextElementSibling?.textContent).toBe('On')
    expect(screen.getByText('Noise suppression').nextElementSibling?.textContent).toBe('Off')
    expect(screen.getByText('Auto gain control').nextElementSibling?.textContent).toBe('Unavailable')
    expect(mocks.createRealtimeVoiceClient).toHaveBeenCalledWith(
      media.stream,
      expect.any(Object),
    )
  })

  it('shows microphone denial and does not create a session', async () => {
    const getUserMedia = vi.fn().mockRejectedValue(new DOMException('denied', 'NotAllowedError'))
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia },
    })
    render(<RealtimeVoicePanel />)

    fireEvent.click(screen.getByRole('button', { name: 'Start a voice call' }))

    expect(await screen.findByText(/Microphone access was denied/i)).toBeTruthy()
    expect(mocks.createRealtimeVoiceSession).not.toHaveBeenCalled()
    expect(mocks.createRealtimeVoiceClient).not.toHaveBeenCalled()
  })

  it('uses a fresh one-use ticket after signaling failure and retry', async () => {
    const media = setupMedia({ echoCancellation: true })
    mocks.createRealtimeVoiceSession
      .mockResolvedValueOnce(session('session-1', 'ticket-one'))
      .mockResolvedValueOnce(session('session-2', 'ticket-two'))
    const clients: Array<{ client: { connect: ReturnType<typeof vi.fn>; disconnect: ReturnType<typeof vi.fn> } }> = []
    mocks.createRealtimeVoiceClient.mockImplementation(() => {
      const attempt = clients.length
      const client = {
        connect: vi.fn(attempt === 0
          ? async () => { throw new Error('offer rejected') }
          : async () => {}),
        disconnect: vi.fn().mockResolvedValue(undefined),
      }
      clients.push({ client })
      return client
    })
    render(<RealtimeVoicePanel />)

    fireEvent.click(screen.getByRole('button', { name: 'Start a voice call' }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('Signaling failed'))
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))

    await screen.findByText('Connected · Speak naturally')
    expect(mocks.createRealtimeVoiceSession).toHaveBeenCalledTimes(2)
    expect(clients[0].client.connect).toHaveBeenCalledWith({
      offerUrl: '/api/v1/realtime/sessions/session-1/offer',
      ticket: 'ticket-one',
      iceServers: [],
    })
    expect(clients[1].client.connect).toHaveBeenCalledWith({
      offerUrl: '/api/v1/realtime/sessions/session-2/offer',
      ticket: 'ticket-two',
      iceServers: [],
    })
    expect(document.body.textContent).not.toContain('ticket-one')
    expect(document.body.textContent).not.toContain('ticket-two')
    expect(mocks.createRealtimeVoiceClient).toHaveBeenCalledWith(media.stream, expect.any(Object))
  })

  it('cleans up the client and microphone before offering retry after a transport error', async () => {
    const media = setupMedia()
    let finishConnect: (() => void) | undefined
    let finishDisconnect: (() => void) | undefined
    const { client, callbacks } = setupClient(() => new Promise<void>((resolve) => {
      finishConnect = resolve
    }))
    client.disconnect.mockImplementation(() => new Promise<void>((resolve) => {
      finishDisconnect = resolve
    }))
    render(<RealtimeVoicePanel />)

    fireEvent.click(screen.getByRole('button', { name: 'Start a voice call' }))
    await waitFor(() => expect(client.connect).toHaveBeenCalledOnce())
    act(() => callbacks()?.onTransportStateChanged?.('error' as never))

    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
    expect(screen.getByRole('button', { name: /Stop voice test/ })).toBeTruthy()
    expect(client.disconnect).toHaveBeenCalledOnce()
    expect(media.track.stop).not.toHaveBeenCalled()

    await act(async () => {
      finishDisconnect?.()
      await Promise.resolve()
    })
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('WebRTC connection reported an error'))
    expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy()
    expect(media.track.stop).toHaveBeenCalledOnce()

    finishConnect?.()
  })

  it('shows the WebRTC error and retries with a fresh session ticket', async () => {
    const captures = Array.from({ length: 2 }, () => {
      const track = {
        enabled: true,
        getSettings: vi.fn(() => ({})),
        stop: vi.fn(),
      } as unknown as MediaStreamTrack
      const stream = {
        getTracks: () => [track],
        getAudioTracks: () => [track],
      } as unknown as MediaStream
      return { track, stream }
    })
    const captureQueue = [...captures]
    const getUserMedia = vi.fn(async () => captureQueue.shift()!.stream)
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia },
    })
    mocks.createRealtimeVoiceSession
      .mockResolvedValueOnce(session('session-error', 'ticket-error'))
      .mockResolvedValueOnce(session('session-retry', 'ticket-retry'))
    const callbackSets: Array<{ onTransportStateChanged: (state: string) => void }> = []
    const clients: Array<{
      connect: ReturnType<typeof vi.fn>
      disconnect: ReturnType<typeof vi.fn>
      setMicrophoneEnabled: ReturnType<typeof vi.fn>
    }> = []
    mocks.createRealtimeVoiceClient.mockImplementation((_stream, callbacks) => {
      callbackSets.push(callbacks)
      const client = {
        connect: vi.fn().mockResolvedValue(undefined),
        disconnect: vi.fn().mockResolvedValue(undefined),
        setMicrophoneEnabled: vi.fn(),
      }
      clients.push(client)
      return client
    })
    render(<RealtimeVoicePanel />)

    fireEvent.click(screen.getByRole('button', { name: 'Start a voice call' }))
    await screen.findByText('Connected · Speak naturally')
    act(() => callbackSets[0].onTransportStateChanged('error'))

    expect((await screen.findByRole('alert')).textContent)
      .toBe('The WebRTC connection reported an error. Retry starts a fresh voice session.')
    expect(clients[0].disconnect).toHaveBeenCalledOnce()
    expect(captures[0].track.stop).toHaveBeenCalledOnce()
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))

    await screen.findByText('Connected · Speak naturally')
    expect(mocks.createRealtimeVoiceSession).toHaveBeenCalledTimes(2)
    expect(clients[1].connect).toHaveBeenCalledWith({
      offerUrl: '/api/v1/realtime/sessions/session-retry/offer',
      ticket: 'ticket-retry',
      iceServers: [],
    })
    fireEvent.click(screen.getByRole('button', { name: /Stop voice test/ }))
    await screen.findAllByText('Call ended')
    expect(captures[1].track.stop).toHaveBeenCalledOnce()
  })

  it('waits for transport-error cleanup when the user stops', async () => {
    const media = setupMedia()
    let finishConnect: (() => void) | undefined
    let finishDisconnect: (() => void) | undefined
    const { client, callbacks } = setupClient(() => new Promise<void>((resolve) => {
      finishConnect = resolve
    }))
    client.disconnect.mockImplementation(() => new Promise<void>((resolve) => {
      finishDisconnect = resolve
    }))
    render(<RealtimeVoicePanel />)

    fireEvent.click(screen.getByRole('button', { name: 'Start a voice call' }))
    await waitFor(() => expect(client.connect).toHaveBeenCalledOnce())
    act(() => callbacks()?.onTransportStateChanged?.('error' as never))
    fireEvent.click(screen.getByRole('button', { name: /Stop voice test/ }))
    await act(async () => { await Promise.resolve() })

    expect(screen.queryByRole('button', { name: 'Start a voice call' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
    expect(media.track.stop).not.toHaveBeenCalled()

    await act(async () => {
      finishDisconnect?.()
      await Promise.resolve()
    })
    await waitFor(() => expect(screen.getAllByText('Call ended').length).toBeGreaterThan(0))
    expect(media.track.stop).toHaveBeenCalledOnce()
    expect(screen.getByRole('button', { name: 'Start a voice call' })).toBeTruthy()

    finishConnect?.()
  })

  it('renders user and assistant transcript events and remote audio', async () => {
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(() => Promise.resolve())
    setupMedia()
    const { callbacks } = setupClient()
    render(<RealtimeVoicePanel />)
    fireEvent.click(screen.getByRole('button', { name: 'Start a voice call' }))
    await screen.findByText('Connected · Speak naturally')

    const remoteStream = { getAudioTracks: () => [{ kind: 'audio' }] } as unknown as MediaStream
    act(() => {
      callbacks()?.onServerMessage?.({
        type: 'conversation.item.input_audio_transcription.completed',
        transcript: 'Hello there',
      } as never)
      callbacks()?.onServerMessage?.({
        type: 'response.done',
        output_text: 'Hello back',
      } as never)
      callbacks()?.onRemoteStream?.(remoteStream as never)
      callbacks()?.onIcePathChanged?.({
        localType: 'relay',
        localProtocol: 'udp',
        localRelayProtocol: 'tls',
        remoteType: 'host',
        remoteProtocol: 'udp',
      } as never)
    })

    expect(screen.getByText('Hello there')).toBeTruthy()
    expect(screen.getByText('Hello back')).toBeTruthy()
    expect(screen.getByText('Local relay over UDP via TURN TLS → remote host over UDP')).toBeTruthy()
    expect((screen.getByLabelText('Assistant audio') as HTMLAudioElement).srcObject).toBe(remoteStream)
  })

  it('disconnects the Pipecat client and stops microphone tracks when stopped', async () => {
    const media = setupMedia()
    const { client } = setupClient()
    render(<RealtimeVoicePanel />)
    fireEvent.click(screen.getByRole('button', { name: 'Start a voice call' }))
    await screen.findByText('Connected · Speak naturally')

    fireEvent.click(screen.getByRole('button', { name: /Stop voice test/ }))

    await waitFor(() => expect(screen.getAllByText('Call ended').length).toBeGreaterThan(0))
    expect(client.disconnect).toHaveBeenCalledOnce()
    expect(media.track.stop).toHaveBeenCalledOnce()
  })

  it('lets the user stop while microphone permission is pending and discards late capture', async () => {
    const media = setupMedia()
    let resolveCapture: ((stream: MediaStream) => void) | undefined
    const delayedGetUserMedia = vi.fn(() => new Promise<MediaStream>((resolve) => {
      resolveCapture = resolve
    }))
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia: delayedGetUserMedia },
    })
    render(<RealtimeVoicePanel />)
    fireEvent.click(screen.getByRole('button', { name: 'Start a voice call' }))

    fireEvent.click(screen.getByRole('button', { name: /Stop voice test/ }))
    await waitFor(() => expect(screen.getAllByText('Call ended').length).toBeGreaterThan(0))
    await act(async () => { resolveCapture?.(media.stream); await Promise.resolve() })

    expect(media.track.stop).toHaveBeenCalledOnce()
    expect(mocks.createRealtimeVoiceSession).not.toHaveBeenCalled()
  })

  it('reports an ICE timeout separately from a signaling failure', async () => {
    vi.useFakeTimers()
    setupMedia()
    setupClient(() => new Promise(() => {}))
    render(<RealtimeVoicePanel />)
    fireEvent.click(screen.getByRole('button', { name: 'Start a voice call' }))
    await act(async () => {
      for (let index = 0; index < 10; index += 1) await Promise.resolve()
    })
    expect(mocks.createRealtimeVoiceClient).toHaveBeenCalledOnce()
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })

    expect(screen.getByRole('alert').textContent).toContain('ICE connection timed out')
  })

  it('does not time out after the transport reports connected while connect remains pending', async () => {
    vi.useFakeTimers()
    setupMedia()
    const { client, callbacks } = setupClient(() => new Promise(() => {}))
    render(<RealtimeVoicePanel />)
    fireEvent.click(screen.getByRole('button', { name: 'Start a voice call' }))
    await act(async () => {
      for (let index = 0; index < 10; index += 1) await Promise.resolve()
    })
    expect(client.connect).toHaveBeenCalledOnce()

    act(() => callbacks()?.onTransportStateChanged?.('connected' as never))
    expect(screen.getByText('Connected · Speak naturally')).toBeTruthy()
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })

    expect(screen.getByText('Connected · Speak naturally')).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(client.disconnect).not.toHaveBeenCalled()
  })

  it('releases the microphone when session creation fails and leaves retry available', async () => {
    const media = setupMedia()
    setupClient()
    mocks.createRealtimeVoiceSession.mockRejectedValueOnce(new Error('gateway unavailable'))
    render(<RealtimeVoicePanel />)

    fireEvent.click(screen.getByRole('button', { name: 'Start a voice call' }))

    expect((await screen.findByRole('alert')).textContent).toContain('Signaling failed')
    expect(media.track.stop).toHaveBeenCalledOnce()
    expect(mocks.createRealtimeVoiceClient).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy()
  })

  it('mutes and unmutes the microphone through the call controls', async () => {
    const media = setupMedia()
    const { client } = setupClient()
    render(<RealtimeVoicePanel />)

    fireEvent.click(screen.getByRole('button', { name: 'Start a voice call' }))
    await screen.findByText('Connected · Speak naturally')

    expect(screen.getByRole('button', { name: 'Mute microphone' }).getAttribute('aria-pressed')).toBe('false')
    fireEvent.click(screen.getByRole('button', { name: 'Mute microphone' }))
    expect(screen.getByRole('button', { name: 'Unmute microphone' }).getAttribute('aria-pressed')).toBe('true')
    expect(client.setMicrophoneEnabled).toHaveBeenNthCalledWith(1, false)

    fireEvent.click(screen.getByRole('button', { name: 'Unmute microphone' }))
    expect(screen.getByRole('button', { name: 'Mute microphone' }).getAttribute('aria-pressed')).toBe('false')
    expect(client.setMicrophoneEnabled).toHaveBeenNthCalledWith(2, true)

    fireEvent.click(screen.getByRole('button', { name: /Stop voice test/ }))
    await waitFor(() => expect(media.track.stop).toHaveBeenCalledOnce())
  })

  it('activates phone call mode while connecting and clears it when the call ends', async () => {
    const onCallModeChange = vi.fn()
    let resolveConnection: (() => void) | undefined
    setupMedia()
    const { client } = setupClient(() => new Promise<void>((resolve) => {
      resolveConnection = resolve
    }))
    render(<RealtimeVoicePanel onCallModeChange={onCallModeChange} />)

    fireEvent.click(screen.getByRole('button', { name: 'Start a voice call' }))
    await waitFor(() => expect(client.connect).toHaveBeenCalledOnce())
    await waitFor(() => expect(onCallModeChange).toHaveBeenLastCalledWith(true))
    expect(screen.getAllByText('Connecting securely').length).toBeGreaterThan(0)

    await act(async () => {
      resolveConnection?.()
      await Promise.resolve()
    })
    await screen.findByText('Connected · Speak naturally')

    fireEvent.click(screen.getByRole('button', { name: /Stop voice test/ }))
    await screen.findAllByText('Call ended')
    await waitFor(() => expect(onCallModeChange).toHaveBeenLastCalledWith(false))
  })

  it('releases call controls and microphone when the remote peer disconnects', async () => {
    const media = setupMedia()
    const { callbacks } = setupClient()
    render(<RealtimeVoicePanel />)

    fireEvent.click(screen.getByRole('button', { name: 'Start a voice call' }))
    await screen.findByText('Connected · Speak naturally')
    act(() => callbacks()?.onDisconnected?.())

    expect((await screen.findByRole('alert')).textContent).toContain('The voice connection ended')
    expect(media.track.stop).toHaveBeenCalledOnce()
    expect(screen.queryByRole('button', { name: /Stop voice test/ })).toBeNull()
    expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy()
  })

  it('ignores queued transport and transcript events after a remote disconnect', async () => {
    setupMedia()
    const { callbacks } = setupClient()
    render(<RealtimeVoicePanel />)
    fireEvent.click(screen.getByRole('button', { name: 'Start a voice call' }))
    await screen.findByText('Connected · Speak naturally')
    const endedCallbacks = callbacks()

    act(() => endedCallbacks?.onDisconnected?.())
    expect((await screen.findByRole('alert')).textContent).toContain('The voice connection ended')
    act(() => {
      endedCallbacks?.onTransportStateChanged?.('connected' as never)
      endedCallbacks?.onServerMessage?.({
        type: 'conversation.item.input_audio_transcription.completed',
        transcript: 'Late remote transcript',
      } as never)
    })

    expect(document.querySelector('.voice-state-pill')?.textContent).toBe('Disconnected')
    expect(screen.queryByText('Connected · Speak naturally')).toBeNull()
    expect(screen.queryByText('Late remote transcript')).toBeNull()
    expect(screen.queryByRole('button', { name: /Stop voice test/ })).toBeNull()
  })

  it('preserves separate user and assistant entries across multiple conversation turns', async () => {
    setupMedia()
    const { callbacks } = setupClient()
    render(<RealtimeVoicePanel />)
    fireEvent.click(screen.getByRole('button', { name: 'Start a voice call' }))
    await screen.findByText('Connected · Speak naturally')

    act(() => {
      callbacks()?.onServerMessage?.({ type: 'response.created' } as never)
      callbacks()?.onServerMessage?.({ type: 'response.output_text.delta', delta: 'First ' } as never)
      callbacks()?.onServerMessage?.({ type: 'response.output_text.delta', delta: 'answer.' } as never)
      callbacks()?.onServerMessage?.({ type: 'response.done', output_text: 'First answer.' } as never)
      callbacks()?.onServerMessage?.({
        type: 'conversation.item.input_audio_transcription.completed',
        transcript: 'What comes next?',
      } as never)
      callbacks()?.onServerMessage?.({ type: 'response.created' } as never)
      callbacks()?.onServerMessage?.({ type: 'response.output_text.delta', delta: 'Second answer.' } as never)
      callbacks()?.onServerMessage?.({ type: 'response.done', output_text: 'Second answer.' } as never)
    })

    const entries = Array.from(screen.getByRole('log').querySelectorAll('article'), (entry) => entry.textContent)
    expect(entries).toEqual([
      'AI AssistantFirst answer.',
      'YouWhat comes next?',
      'AI AssistantSecond answer.',
    ])
  })

  it('ignores transcript events from a stopped call after a new call starts', async () => {
    setupMedia()
    const { callbacks } = setupClient()
    render(<RealtimeVoicePanel />)
    fireEvent.click(screen.getByRole('button', { name: 'Start a voice call' }))
    await screen.findByText('Connected · Speak naturally')
    const oldCallbacks = callbacks()
    fireEvent.click(screen.getByRole('button', { name: /Stop voice test/ }))
    await screen.findAllByText('Call ended')
    fireEvent.click(screen.getByRole('button', { name: 'Start a voice call' }))
    await screen.findByText('Connected · Speak naturally')

    act(() => {
      oldCallbacks?.onServerMessage?.({
        type: 'conversation.item.input_audio_transcription.completed',
        transcript: 'Stale previous call',
      } as never)
      oldCallbacks?.onServerMessage?.({ type: 'response.created' } as never)
      oldCallbacks?.onServerMessage?.({ type: 'response.output_text.delta', delta: 'Stale reply' } as never)
      callbacks()?.onServerMessage?.({
        type: 'conversation.item.input_audio_transcription.completed',
        transcript: 'Current call',
      } as never)
    })

    expect(screen.queryByText('Stale previous call')).toBeNull()
    expect(screen.queryByText('Stale reply')).toBeNull()
    expect(screen.getByRole('log').querySelectorAll('article')).toHaveLength(1)
    expect(screen.getByText('Current call')).toBeTruthy()
    expect(screen.getByText('Connected · Speak naturally')).toBeTruthy()
  })

  it('releases the active call when the panel is unmounted', async () => {
    const media = setupMedia()
    const { client } = setupClient()
    const onCallModeChange = vi.fn()
    const view = render(<RealtimeVoicePanel onCallModeChange={onCallModeChange} />)
    fireEvent.click(screen.getByRole('button', { name: 'Start a voice call' }))
    await screen.findByText('Connected · Speak naturally')

    view.unmount()
    await act(async () => { await Promise.resolve() })

    expect(client.disconnect).toHaveBeenCalledOnce()
    expect(media.track.stop).toHaveBeenCalledOnce()
    expect(onCallModeChange).toHaveBeenLastCalledWith(false)
  })

  it('discards microphone capture that resolves after the panel is unmounted', async () => {
    const media = setupMedia()
    let resolveCapture: ((stream: MediaStream) => void) | undefined
    media.getUserMedia.mockImplementation(() => new Promise<MediaStream>((resolve) => {
      resolveCapture = resolve
    }))
    const view = render(<RealtimeVoicePanel />)
    fireEvent.click(screen.getByRole('button', { name: 'Start a voice call' }))
    expect(media.getUserMedia).toHaveBeenCalledOnce()

    view.unmount()
    await act(async () => {
      resolveCapture?.(media.stream)
      await Promise.resolve()
    })

    expect(media.track.stop).toHaveBeenCalledOnce()
    expect(mocks.createRealtimeVoiceSession).not.toHaveBeenCalled()
    expect(mocks.createRealtimeVoiceClient).not.toHaveBeenCalled()
  })

  it('discards a session that resolves after the panel is unmounted', async () => {
    const media = setupMedia()
    let resolveSession: ((value: ReturnType<typeof session>) => void) | undefined
    mocks.createRealtimeVoiceSession.mockImplementation(() => new Promise((resolve) => {
      resolveSession = resolve
    }))
    const view = render(<RealtimeVoicePanel />)
    fireEvent.click(screen.getByRole('button', { name: 'Start a voice call' }))
    await waitFor(() => expect(mocks.createRealtimeVoiceSession).toHaveBeenCalledOnce())

    view.unmount()
    await act(async () => {
      resolveSession?.(session('abandoned-session', 'abandoned-ticket'))
      await Promise.resolve()
    })

    expect(media.track.stop).toHaveBeenCalledOnce()
    expect(mocks.createRealtimeVoiceClient).not.toHaveBeenCalled()
  })

  it('offers an audio retry after autoplay is blocked and clears the warning when playback starts', async () => {
    const play = vi.spyOn(HTMLMediaElement.prototype, 'play')
      .mockImplementationOnce(() => Promise.reject(new Error('autoplay blocked')))
      .mockImplementation(() => Promise.resolve())
    setupMedia()
    const { callbacks } = setupClient()
    render(<RealtimeVoicePanel />)
    fireEvent.click(screen.getByRole('button', { name: 'Start a voice call' }))
    await screen.findByText('Connected · Speak naturally')
    const remoteStream = { getAudioTracks: () => [{ kind: 'audio' }] } as unknown as MediaStream

    act(() => callbacks()?.onRemoteStream?.(remoteStream as never))
    expect(await screen.findByText('Tap to enable assistant audio.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Enable audio' }))

    await waitFor(() => expect(play).toHaveBeenCalledTimes(2))
    expect(screen.queryByText('Tap to enable assistant audio.')).toBeNull()
  })
})
