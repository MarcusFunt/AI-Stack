import { useEffect, useRef, useState } from 'react'
import {
  AudioLines, ChevronDown, Clock3, Mic, MicOff, PhoneOff, Volume2,
} from 'lucide-react'
import { localAI } from './api'
import {
  createRealtimeVoiceClient,
  requestVoiceMicrophone,
  type RealtimeVoiceCallbacks,
  type RealtimeVoiceClient,
  type RealtimeIcePath,
} from './realtimeVoice'
import './App.css'

type VoiceState = 'idle' | 'permission' | 'connecting' | 'connected'
  | 'signaling-failure' | 'ice-timeout' | 'disconnected' | 'stopped'

type ConversationEntry = {
  id: number
  role: 'user' | 'assistant'
  text: string
}

type RealtimeVoicePanelProps = {
  onCallModeChange?: (active: boolean) => void
}

const ICE_TIMEOUT_MS = 30_000

function settingLabel(value: boolean | string | undefined): string {
  if (value === undefined) return 'Unavailable'
  if (value === true) return 'On'
  if (value === false) return 'Off'
  return value
}

function stopTracks(stream: MediaStream | null) {
  stream?.getTracks().forEach((track) => track.stop())
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object'
}

function formatDuration(totalSeconds: number): string {
  const minutes = Math.floor(totalSeconds / 60).toString().padStart(2, '0')
  const seconds = (totalSeconds % 60).toString().padStart(2, '0')
  return `${minutes}:${seconds}`
}

function appendAssistantText(
  entries: ConversationEntry[],
  text: string,
  newId: number,
): ConversationEntry[] {
  const next = [...entries]
  for (let index = next.length - 1; index >= 0; index -= 1) {
    if (next[index].role === 'assistant') {
      next[index] = { ...next[index], text: next[index].text + text }
      return next
    }
  }
  return [...next, { id: newId, role: 'assistant', text }]
}

function replaceAssistantText(
  entries: ConversationEntry[],
  text: string,
  newId: number,
): ConversationEntry[] {
  const next = [...entries]
  for (let index = next.length - 1; index >= 0; index -= 1) {
    if (next[index].role === 'assistant') {
      next[index] = { ...next[index], text }
      return next
    }
  }
  return [...next, { id: newId, role: 'assistant', text }]
}

function icePathLabel(icePath: RealtimeIcePath | null, connected: boolean): string {
  if (!icePath) return connected ? 'Waiting for selected candidate pair' : 'Not connected'
  return `Local ${icePath.localType} over ${icePath.localProtocol.toUpperCase()}${icePath.localRelayProtocol ? ` via TURN ${icePath.localRelayProtocol.toUpperCase()}` : ''} → remote ${icePath.remoteType} over ${icePath.remoteProtocol.toUpperCase()}`
}

export default function RealtimeVoicePanel({ onCallModeChange }: RealtimeVoicePanelProps) {
  const [voiceState, setVoiceState] = useState<VoiceState>('idle')
  const [errorMessage, setErrorMessage] = useState('')
  const [micSettings, setMicSettings] = useState<MediaTrackSettings | null>(null)
  const [conversation, setConversation] = useState<ConversationEntry[]>([])
  const [assistantResponding, setAssistantResponding] = useState(false)
  const [microphoneMuted, setMicrophoneMuted] = useState(false)
  const [conversationOpen, setConversationOpen] = useState(false)
  const [playbackBlocked, setPlaybackBlocked] = useState(false)
  const [liveAnnouncement, setLiveAnnouncement] = useState('')
  const [elapsedSeconds, setElapsedSeconds] = useState(0)
  const [icePath, setIcePath] = useState<RealtimeIcePath | null>(null)
  const [remoteStream, setRemoteStream] = useState<MediaStream | null>(null)
  const clientRef = useRef<RealtimeVoiceClient | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const cleanupRef = useRef<Promise<void> | null>(null)
  const attemptRef = useRef(0)
  const stateRef = useRef<VoiceState>('idle')
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const transcriptRef = useRef<HTMLDivElement | null>(null)
  const messageIdRef = useRef(0)
  const shouldAutoScrollRef = useRef(true)

  const updateState = (next: VoiceState) => {
    stateRef.current = next
    setVoiceState(next)
  }

  const handleServerMessage = (value: unknown) => {
    if (!isRecord(value) || typeof value.type !== 'string') return
    if (
      value.type === 'conversation.item.input_audio_transcription.completed'
      && typeof value.transcript === 'string'
    ) {
      const entry: ConversationEntry = {
        id: ++messageIdRef.current,
        role: 'user',
        text: value.transcript,
      }
      setConversation((current) => [...current, entry])
      setLiveAnnouncement(`You said: ${value.transcript}`)
    } else if (value.type === 'response.output_text.delta' && typeof value.delta === 'string') {
      const id = ++messageIdRef.current
      setConversation((current) => appendAssistantText(current, value.delta as string, id))
    } else if (value.type === 'response.done') {
      if (typeof value.output_text === 'string') {
        const id = ++messageIdRef.current
        setConversation((current) => replaceAssistantText(current, value.output_text as string, id))
        setLiveAnnouncement(`Assistant: ${value.output_text}`)
      }
      setAssistantResponding(false)
    } else if (value.type === 'response.created') {
      setAssistantResponding(true)
      const entry: ConversationEntry = {
        id: ++messageIdRef.current,
        role: 'assistant',
        text: '',
      }
      setConversation((current) => [...current, entry])
    }
  }

  const stop = async () => {
    attemptRef.current += 1
    const client = clientRef.current
    const stream = streamRef.current
    const pendingCleanup = cleanupRef.current
    clientRef.current = null
    streamRef.current = null
    setRemoteStream(null)
    setPlaybackBlocked(false)
    setIcePath(null)
    setErrorMessage('')
    setMicrophoneMuted(false)
    setAssistantResponding(false)
    setConversationOpen(false)
    try {
      await client?.disconnect()
    } catch {
      // Media tracks are still stopped even if the transport was already closed.
    } finally {
      stopTracks(stream)
      await pendingCleanup?.catch(() => {})
      updateState('stopped')
    }
  }

  const start = async () => {
    const attempt = ++attemptRef.current
    setErrorMessage('')
    setMicSettings(null)
    setConversation([])
    messageIdRef.current = 0
    setAssistantResponding(false)
    setMicrophoneMuted(false)
    setConversationOpen(false)
    setPlaybackBlocked(false)
    setLiveAnnouncement('')
    setElapsedSeconds(0)
    setRemoteStream(null)
    setIcePath(null)
    updateState('permission')

    let stream: MediaStream | null = null
    let client: RealtimeVoiceClient | null = null
    try {
      try {
        stream = await requestVoiceMicrophone()
      } catch (error) {
        if (attempt !== attemptRef.current) return
        updateState('permission')
        const name = isRecord(error) && typeof error.name === 'string' ? error.name : ''
        setErrorMessage(name === 'NotAllowedError' || name === 'PermissionDeniedError'
          ? 'Microphone access was denied. Allow microphone access in your browser settings, then try again.'
          : name === 'NotFoundError'
            ? 'No microphone is available in this browser. Connect a microphone and try again.'
            : 'Microphone access could not be started. Check your browser settings and try again.')
        return
      }
      if (attempt !== attemptRef.current) {
        stopTracks(stream)
        return
      }
      streamRef.current = stream
      const settings = stream.getAudioTracks()[0]?.getSettings?.()
      setMicSettings(settings || {})
      updateState('connecting')

      const session = await localAI.createRealtimeVoiceSession()
      if (attempt !== attemptRef.current) return

      let resolveTransportConnected: (() => void) | undefined
      let rejectTransportConnection: ((error: Error) => void) | undefined
      const transportConnected = new Promise<void>((resolve, reject) => {
        resolveTransportConnected = resolve
        rejectTransportConnection = reject
      })

      const callbacks: RealtimeVoiceCallbacks = {
        onTransportStateChanged: (state) => {
          if (attempt !== attemptRef.current) return
          if (state === 'connected' || state === 'ready') {
            updateState('connected')
            resolveTransportConnected?.()
          }
          if (state === 'error') {
            rejectTransportConnection?.(new Error('voice-transport-error'))
            const cleanupAttempt = ++attemptRef.current
            const activeClient = clientRef.current ?? client
            const activeStream = streamRef.current ?? stream
            clientRef.current = null
            streamRef.current = null
            setRemoteStream(null)
            setPlaybackBlocked(false)
            setIcePath(null)
            setMicrophoneMuted(false)
            const cleanup = (async () => {
              try {
                await activeClient?.disconnect()
              } catch {
                // Still release the microphone when the transport is already closed.
              } finally {
                stopTracks(activeStream)
              }
              if (attemptRef.current !== cleanupAttempt) return
              updateState('signaling-failure')
              setErrorMessage('The WebRTC connection reported an error. Retry starts a fresh voice session.')
            })()
            cleanupRef.current = cleanup
            const clearCleanup = () => {
              if (cleanupRef.current === cleanup) cleanupRef.current = null
            }
            void cleanup.then(clearCleanup, clearCleanup)
          }
        },
        onDisconnected: () => {
          if (attempt !== attemptRef.current || stateRef.current !== 'connected') return
          clientRef.current = null
          stopTracks(streamRef.current)
          streamRef.current = null
          setRemoteStream(null)
          setPlaybackBlocked(false)
          setIcePath(null)
          setMicrophoneMuted(false)
          updateState('disconnected')
          setErrorMessage('The voice connection ended. Check the network connection and retry.')
        },
        onServerMessage: handleServerMessage,
        onRemoteStream: (next) => {
          if (attempt === attemptRef.current) {
            setRemoteStream(next)
            if (!next) setPlaybackBlocked(false)
          }
        },
        onIcePathChanged: (path) => {
          if (attempt === attemptRef.current) setIcePath(path)
        },
      }
      client = createRealtimeVoiceClient(stream, callbacks)
      clientRef.current = client

      let timeoutId: number | undefined
      const timeout = new Promise<never>((_resolve, reject) => {
        timeoutId = window.setTimeout(() => reject(new Error('voice-ice-timeout')), ICE_TIMEOUT_MS)
      })
      try {
        void client.connect({
            offerUrl: session.offer_url,
            ticket: session.client_secret.value,
            iceServers: session.ice_servers,
          }).then(
            () => resolveTransportConnected?.(),
            (error: unknown) => rejectTransportConnection?.(
              error instanceof Error ? error : new Error('voice-signaling-failed'),
            ),
          )
        await Promise.race([transportConnected, timeout])
        if (attempt === attemptRef.current) updateState('connected')
      } finally {
        if (timeoutId !== undefined) window.clearTimeout(timeoutId)
      }
    } catch (error) {
      if (attempt !== attemptRef.current) return
      const timedOut = error instanceof Error && error.message === 'voice-ice-timeout'
      const nextState: VoiceState = timedOut ? 'ice-timeout' : 'signaling-failure'
      updateState(nextState)
      setErrorMessage(timedOut
        ? 'ICE connection timed out. Check the network path and try again.'
        : 'Signaling failed. Retry starts a fresh voice session.')
      clientRef.current = null
      try {
        await client?.disconnect()
      } catch {
        // Stop the capture track below even if signaling did not finish cleanly.
      }
      if (streamRef.current === stream) streamRef.current = null
      stopTracks(stream)
    }
  }

  const toggleMicrophone = () => {
    if (!connected) return
    const nextMuted = !microphoneMuted
    clientRef.current?.setMicrophoneEnabled(!nextMuted)
    setMicrophoneMuted(nextMuted)
  }

  const retryAudioPlayback = async () => {
    try {
      await audioRef.current?.play()
      setPlaybackBlocked(false)
    } catch {
      setPlaybackBlocked(true)
    }
  }

  const handleTranscriptScroll = () => {
    const transcript = transcriptRef.current
    if (!transcript) return
    shouldAutoScrollRef.current = transcript.scrollHeight
      - transcript.scrollTop
      - transcript.clientHeight < 56
  }

  useEffect(() => {
    const audio = audioRef.current
    if (!audio) return
    audio.srcObject = remoteStream
    if (!remoteStream) return
    void audio.play().then(() => setPlaybackBlocked(false)).catch(() => setPlaybackBlocked(true))
  }, [remoteStream])

  useEffect(() => {
    const transcript = transcriptRef.current
    if (transcript && shouldAutoScrollRef.current) transcript.scrollTop = transcript.scrollHeight
  }, [conversation, conversationOpen])

  const busy = (voiceState === 'permission' || voiceState === 'connecting') && !errorMessage
  const connected = voiceState === 'connected'

  useEffect(() => {
    onCallModeChange?.(busy || connected)
  }, [busy, connected, onCallModeChange])

  useEffect(() => () => onCallModeChange?.(false), [onCallModeChange])

  useEffect(() => {
    if (!connected) return
    const startedAt = Date.now()
    const updateElapsed = () => setElapsedSeconds(Math.floor((Date.now() - startedAt) / 1000))
    updateElapsed()
    const timer = window.setInterval(updateElapsed, 1000)
    return () => window.clearInterval(timer)
  }, [connected])

  const statusLabels: Record<VoiceState, string> = {
    idle: 'Ready for a voice call',
    permission: 'Requesting microphone',
    connecting: 'Connecting securely',
    connected: 'Connected',
    'signaling-failure': 'Connection failed',
    'ice-timeout': 'Network connection timed out',
    disconnected: 'Disconnected',
    stopped: 'Call ended',
  }
  const callStatus = busy
    ? statusLabels[voiceState]
    : connected
      ? assistantResponding ? 'Assistant is replying' : 'Connected · Speak naturally'
      : statusLabels[voiceState]
  const callModeClass = busy || connected ? ' voice-call-mode' : ''
  const conversationClass = conversationOpen ? ' voice-conversation-open' : ''

  return (
    <section className={'workspace realtime-voice-workspace' + callModeClass + conversationClass}>
      <div className="workspace-head voice-page-heading">
        <div>
          <span className="eyebrow">REALTIME VOICE</span>
          <h2>Voice call</h2>
          <p>Try a natural two-way voice conversation in your browser.</p>
        </div>
        <span className={'voice-state-pill voice-state-' + voiceState} role="status" aria-live="polite">
          {voiceState === 'permission' && errorMessage ? 'Microphone permission required' : statusLabels[voiceState]}
        </span>
      </div>

      <div className="voice-grid">
        <div className="tool-card voice-call-card">
          <div className="voice-card-heading">
            <div><span className="eyebrow">BROWSER SESSION</span><h3>Call controls</h3></div>
            <span className={'voice-state-pill voice-state-' + voiceState} aria-hidden="true">
              {statusLabels[voiceState]}
            </span>
          </div>

          <div className="voice-call-identity">
            <div className="voice-call-orb-wrap">
              <div className={'voice-orb' + (connected ? assistantResponding ? ' is-speaking' : ' is-connected' : busy ? ' is-connecting' : '')} aria-hidden="true">
                <span className="voice-orb-ring voice-orb-ring-one" />
                <span className="voice-orb-ring voice-orb-ring-two" />
                <span className="voice-orb-core"><AudioLines size={34} strokeWidth={1.6} /></span>
              </div>
            </div>
            <h3 className="voice-assistant-name">AI Assistant</h3>
            <p className="voice-call-status">{callStatus}</p>
            <p className="voice-call-duration">
              {connected && <><Clock3 size={14} />{formatDuration(elapsedSeconds)}</>}
              {busy && <><span className="voice-status-dot" />{voiceState === 'permission' ? 'Waiting for microphone access' : 'Setting up your call'}</>}
            </p>
          </div>

          <p className="voice-help">Allow microphone access to begin. Your words appear after each turn is recognized, and assistant replies stream as they are generated.</p>
          {errorMessage && <div className="voice-error" role="alert">{errorMessage}</div>}
          {playbackBlocked && connected && (
            <div className="voice-playback-warning" role="status">
              <span>Tap to enable assistant audio.</span>
              <button className="secondary no-margin" onClick={() => void retryAudioPlayback()}>Enable audio</button>
            </div>
          )}

          <div className="voice-actions">
            {connected && (
              <button
                className={'voice-round-action voice-mute-action' + (microphoneMuted ? ' is-muted' : '')}
                onClick={toggleMicrophone}
                aria-pressed={microphoneMuted}
                aria-label={microphoneMuted ? 'Unmute microphone' : 'Mute microphone'}
              >
                {microphoneMuted ? <MicOff size={21} /> : <Mic size={21} />}
                <span>{microphoneMuted ? 'Unmute' : 'Mute'}</span>
              </button>
            )}

            {connected && (
              <button
                className="voice-round-action voice-conversation-toggle"
                onClick={() => setConversationOpen((open) => !open)}
                aria-expanded={conversationOpen}
                aria-controls="voice-conversation-panel"
              >
                <AudioLines size={21} />
                <span>{conversationOpen ? 'Hide text' : 'Conversation'}</span>
              </button>
            )}

            {!busy && !connected && (
              <button className="primary no-margin voice-start-button" onClick={() => void start()}>
                <Mic size={18} />{voiceState === 'idle' || voiceState === 'stopped' ? 'Start a voice call' : 'Try again'}
              </button>
            )}

            {(busy || connected) && (
              <button className="danger-button no-margin voice-end-button" onClick={() => void stop()}>
                <PhoneOff size={19} />
                <span className="voice-desktop-action-label">Stop voice test</span>
                <span className="voice-mobile-action-label">End call</span>
              </button>
            )}
          </div>

          <div className="voice-sr-status" role="status" aria-live="polite" aria-atomic="true">
            {liveAnnouncement}
          </div>
        </div>

        <section
          className="tool-card voice-transcript-card"
          id="voice-conversation-panel"
          aria-label="Conversation and connection details"
        >
          <div className="voice-card-heading voice-transcript-heading">
            <div><span className="eyebrow">SESSION TRANSCRIPT</span><h3>Conversation</h3></div>
            <button className="voice-conversation-close" onClick={() => setConversationOpen(false)} aria-label="Close conversation">
              <ChevronDown size={20} />
            </button>
            <Volume2 size={19} aria-hidden="true" />
          </div>
          <div className="voice-transcript" ref={transcriptRef} onScroll={handleTranscriptScroll} role="log" aria-live="off" aria-relevant="additions text">
            {conversation.map((entry) => (
              <article className={'voice-message voice-message-' + entry.role} key={entry.id}>
                <strong>{entry.role === 'user' ? 'You' : 'AI Assistant'}</strong>
                <p>{entry.text || (assistantResponding ? 'Thinking through a reply…' : 'No text returned for this reply.')}</p>
              </article>
            ))}
            {conversation.length === 0 && (
              <p className="voice-empty">Your conversation appears here. User text is shown after each turn is recognized.</p>
            )}
          </div>
          <audio
            ref={audioRef}
            aria-label="Assistant audio"
            autoPlay
            controls
            playsInline
            onPlaying={() => setPlaybackBlocked(false)}
          />
          <div className="voice-audio-state"><Volume2 size={15} />Assistant speech plays through this browser.</div>

          <details className="voice-details">
            <summary><span>Microphone &amp; connection details</span><ChevronDown size={16} /></summary>
            <div className="voice-settings" aria-label="Microphone processing settings">
              <div><span>Echo cancellation</span><strong>{settingLabel(micSettings?.echoCancellation)}</strong></div>
              <div><span>Noise suppression</span><strong>{settingLabel(micSettings?.noiseSuppression)}</strong></div>
              <div><span>Auto gain control</span><strong>{settingLabel(micSettings?.autoGainControl)}</strong></div>
            </div>
            <div className="voice-audio-state voice-ice-path" aria-live="polite">
              <span className="voice-detail-label">Selected ICE path</span>
              <span>{icePathLabel(icePath, connected)}</span>
            </div>
            <p className="voice-privacy-note">The short-lived session ticket stays in the signaling request header. The browser never receives the gateway key or TURN secret.</p>
          </details>
        </section>
      </div>
    </section>
  )
}
