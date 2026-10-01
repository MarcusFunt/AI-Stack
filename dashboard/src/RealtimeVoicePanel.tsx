import { useEffect, useRef, useState } from 'react'
import { Mic, Square, Volume2 } from 'lucide-react'
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

const ICE_TIMEOUT_MS = 30_000

function settingLabel(value: boolean | string | undefined): string {
  if (value === undefined) return 'unavailable'
  if (value === true) return 'on'
  if (value === false) return 'off'
  return value
}

function stopTracks(stream: MediaStream | null) {
  stream?.getTracks().forEach((track) => track.stop())
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object'
}

export default function RealtimeVoicePanel() {
  const [voiceState, setVoiceState] = useState<VoiceState>('idle')
  const [errorMessage, setErrorMessage] = useState('')
  const [micSettings, setMicSettings] = useState<MediaTrackSettings | null>(null)
  const [userTranscript, setUserTranscript] = useState('')
  const [assistantTranscript, setAssistantTranscript] = useState('')
  const [icePath, setIcePath] = useState<RealtimeIcePath | null>(null)
  const [remoteStream, setRemoteStream] = useState<MediaStream | null>(null)
  const clientRef = useRef<RealtimeVoiceClient | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const cleanupRef = useRef<Promise<void> | null>(null)
  const attemptRef = useRef(0)
  const stateRef = useRef<VoiceState>('idle')
  const audioRef = useRef<HTMLAudioElement | null>(null)

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
      setUserTranscript(value.transcript)
    } else if (value.type === 'response.output_text.delta' && typeof value.delta === 'string') {
      setAssistantTranscript((current) => current + value.delta)
    } else if (value.type === 'response.done' && typeof value.output_text === 'string') {
      setAssistantTranscript(value.output_text)
    } else if (value.type === 'response.created') {
      setAssistantTranscript('')
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
    setIcePath(null)
    setErrorMessage('')
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
    setUserTranscript('')
    setAssistantTranscript('')
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
          ? 'Microphone access was denied. Allow microphone access and retry.'
          : name === 'NotFoundError'
            ? 'No microphone is available in this browser.'
            : 'Microphone access could not be started.')
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

      const callbacks: RealtimeVoiceCallbacks = {
        onTransportStateChanged: (state) => {
          if (attempt !== attemptRef.current) return
          if (state === 'connected' || state === 'ready') updateState('connected')
          if (state === 'error') {
            const cleanupAttempt = ++attemptRef.current
            const activeClient = clientRef.current ?? client
            const activeStream = streamRef.current ?? stream
            clientRef.current = null
            streamRef.current = null
            setRemoteStream(null)
            setIcePath(null)
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
              setErrorMessage('The WebRTC connection reported an error. Retry starts a fresh session.')
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
          setIcePath(null)
          updateState('disconnected')
          setErrorMessage('The voice connection ended.')
        },
        onServerMessage: handleServerMessage,
        onRemoteStream: (next) => {
          if (attempt === attemptRef.current) setRemoteStream(next)
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
        await Promise.race([
          client.connect({
            offerUrl: session.offer_url,
            ticket: session.client_secret.value,
            iceServers: session.ice_servers,
          }),
          timeout,
        ])
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

  useEffect(() => {
    const audio = audioRef.current
    if (audio) audio.srcObject = remoteStream
  }, [remoteStream])

  useEffect(() => () => {
    attemptRef.current += 1
    void clientRef.current?.disconnect().catch(() => {})
    stopTracks(streamRef.current)
    clientRef.current = null
    streamRef.current = null
  }, [])

  const statusLabels: Record<VoiceState, string> = {
    idle: 'Idle',
    permission: 'Requesting microphone',
    connecting: 'Connecting',
    connected: 'Connected',
    'signaling-failure': 'Signaling failed',
    'ice-timeout': 'ICE connection timed out',
    disconnected: 'Disconnected',
    stopped: 'Stopped',
  }
  const busy = (voiceState === 'permission' || voiceState === 'connecting') && !errorMessage
  const connected = voiceState === 'connected'
  return (
    <section className="workspace realtime-voice-workspace">
      <div className="workspace-head">
        <div><span className="eyebrow">REALTIME VOICE</span><h2>Test browser voice over WebRTC.</h2></div>
      </div>
      <div className="voice-grid">
        <div className="tool-card voice-call-card">
          <div className="voice-card-heading">
            <div><span className="eyebrow">BROWSER SESSION</span><h3>Microphone and connection</h3></div>
            <span className={'voice-state-pill voice-state-' + voiceState} role="status" aria-live="polite">
              {voiceState === 'permission' && errorMessage ? 'Microphone permission required' : statusLabels[voiceState]}
            </span>
          </div>
          <p className="voice-help">The browser asks for microphone access, then negotiates WebRTC through the authenticated Dashboard proxy.</p>
          <div className="voice-actions">
            {!busy && !connected && <button className="primary no-margin" onClick={() => void start()}>
              <Mic size={15} />{voiceState === 'idle' || voiceState === 'stopped' ? 'Start voice test' : 'Retry'}
            </button>}
            {(busy || connected) && <button className="danger-button no-margin" onClick={() => void stop()}>
              <Square size={14} />Stop voice test
            </button>}
          </div>
          {errorMessage && <div className="voice-error" role="alert">{errorMessage}</div>}
          <div className="voice-settings" aria-label="Microphone processing settings">
            <div><span>Echo cancellation: {settingLabel(micSettings?.echoCancellation)}</span></div>
            <div><span>Noise suppression: {settingLabel(micSettings?.noiseSuppression)}</span></div>
            <div><span>Auto gain control: {settingLabel(micSettings?.autoGainControl)}</span></div>
          </div>
        </div>

        <div className="tool-card voice-transcript-card">
          <div className="voice-card-heading">
            <div><span className="eyebrow">LIVE OUTPUT</span><h3>Conversation</h3></div>
            <Volume2 size={18} aria-hidden="true" />
          </div>
          <div className="voice-transcript" aria-live="polite">
            {userTranscript && <p><strong>You</strong><span>{userTranscript}</span></p>}
            {assistantTranscript && <p><strong>Assistant</strong><span>{assistantTranscript}</span></p>}
            {!userTranscript && !assistantTranscript && <p className="voice-empty">Transcript text appears here during a call.</p>}
          </div>
          <audio ref={audioRef} aria-label="Assistant audio" autoPlay controls playsInline />
          <div className="voice-audio-state"><Volume2 size={14} /> Assistant speech plays through this browser.</div>
          <div className="voice-audio-state" aria-live="polite">
            ICE path: {icePath
              ? `local ${icePath.localType} over ${icePath.localProtocol.toUpperCase()} → remote ${icePath.remoteType} over ${icePath.remoteProtocol.toUpperCase()}`
              : 'waiting for selected candidate pair'}
          </div>
        </div>
      </div>
      <p className="voice-privacy-note">A short-lived session ticket stays in the signaling request header. The test page does not expose the gateway key or TURN secret.</p>
    </section>
  )
}
