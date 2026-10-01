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
  const client = { connect: vi.fn(connect), disconnect: vi.fn().mockResolvedValue(undefined) }
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

    fireEvent.click(screen.getByRole('button', { name: 'Start voice test' }))

    await waitFor(() => expect(mocks.createRealtimeVoiceSession).toHaveBeenCalledOnce())
    expect(media.getUserMedia).toHaveBeenCalledWith({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    })
    expect(screen.getByText('Echo cancellation: on')).toBeTruthy()
    expect(screen.getByText('Noise suppression: off')).toBeTruthy()
    expect(screen.getByText('Auto gain control: unavailable')).toBeTruthy()
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

    fireEvent.click(screen.getByRole('button', { name: 'Start voice test' }))

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

    fireEvent.click(screen.getByRole('button', { name: 'Start voice test' }))
    await waitFor(() => expect(screen.getByRole('status').textContent).toContain('Signaling failed'))
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

    await waitFor(() => expect(screen.getByText('Connected')).toBeTruthy())
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

    fireEvent.click(screen.getByRole('button', { name: 'Start voice test' }))
    await waitFor(() => expect(client.connect).toHaveBeenCalledOnce())
    act(() => callbacks()?.onTransportStateChanged?.('error' as never))

    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Stop voice test' })).toBeTruthy()
    expect(client.disconnect).toHaveBeenCalledOnce()
    expect(media.track.stop).not.toHaveBeenCalled()

    await act(async () => {
      finishDisconnect?.()
      await Promise.resolve()
    })
    await waitFor(() => expect(screen.getByRole('status').textContent).toContain('Signaling failed'))
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy()
    expect(media.track.stop).toHaveBeenCalledOnce()

    finishConnect?.()
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

    fireEvent.click(screen.getByRole('button', { name: 'Start voice test' }))
    await waitFor(() => expect(client.connect).toHaveBeenCalledOnce())
    act(() => callbacks()?.onTransportStateChanged?.('error' as never))
    fireEvent.click(screen.getByRole('button', { name: 'Stop voice test' }))
    await act(async () => { await Promise.resolve() })

    expect(screen.queryByRole('button', { name: 'Start voice test' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
    expect(media.track.stop).not.toHaveBeenCalled()

    await act(async () => {
      finishDisconnect?.()
      await Promise.resolve()
    })
    await waitFor(() => expect(screen.getByText('Stopped')).toBeTruthy())
    expect(media.track.stop).toHaveBeenCalledOnce()
    expect(screen.getByRole('button', { name: 'Start voice test' })).toBeTruthy()

    finishConnect?.()
  })

  it('renders user and assistant transcript events and remote audio', async () => {
    setupMedia()
    const { callbacks } = setupClient()
    render(<RealtimeVoicePanel />)
    fireEvent.click(screen.getByRole('button', { name: 'Start voice test' }))
    await waitFor(() => expect(screen.getByText('Connected')).toBeTruthy())

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
        remoteType: 'host',
        remoteProtocol: 'udp',
      } as never)
    })

    expect(screen.getByText('Hello there')).toBeTruthy()
    expect(screen.getByText('Hello back')).toBeTruthy()
    expect(screen.getByText('ICE path: local relay over UDP → remote host over UDP')).toBeTruthy()
    expect((screen.getByLabelText('Assistant audio') as HTMLAudioElement).srcObject).toBe(remoteStream)
  })

  it('disconnects the Pipecat client and stops microphone tracks when stopped', async () => {
    const media = setupMedia()
    const { client } = setupClient()
    render(<RealtimeVoicePanel />)
    fireEvent.click(screen.getByRole('button', { name: 'Start voice test' }))
    await waitFor(() => expect(screen.getByText('Connected')).toBeTruthy())

    fireEvent.click(screen.getByRole('button', { name: 'Stop voice test' }))

    await waitFor(() => expect(screen.getByText('Stopped')).toBeTruthy())
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
    fireEvent.click(screen.getByRole('button', { name: 'Start voice test' }))

    fireEvent.click(screen.getByRole('button', { name: 'Stop voice test' }))
    await waitFor(() => expect(screen.getByRole('status').textContent).toContain('Stopped'))
    await act(async () => { resolveCapture?.(media.stream); await Promise.resolve() })

    expect(media.track.stop).toHaveBeenCalledOnce()
    expect(mocks.createRealtimeVoiceSession).not.toHaveBeenCalled()
  })

  it('reports an ICE timeout separately from a signaling failure', async () => {
    vi.useFakeTimers()
    setupMedia()
    setupClient(() => new Promise(() => {}))
    render(<RealtimeVoicePanel />)
    fireEvent.click(screen.getByRole('button', { name: 'Start voice test' }))
    await act(async () => {
      for (let index = 0; index < 10; index += 1) await Promise.resolve()
    })
    expect(mocks.createRealtimeVoiceClient).toHaveBeenCalledOnce()
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })

    expect(screen.getByRole('status').textContent).toContain('ICE connection timed out')
  })
})
