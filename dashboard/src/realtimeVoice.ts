import { PipecatClient } from '@pipecat-ai/client-js'
import {
  SmallWebRTCTransport,
  type SmallWebRTCTransportConstructorOptions,
} from '@pipecat-ai/small-webrtc-transport'
import type { RealtimeIceRoute } from './api'

export const VOICE_MICROPHONE_CONSTRAINTS: MediaStreamConstraints = {
  audio: {
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: true,
  },
}

export type RealtimeVoiceConnection = {
  offerUrl: string
  ticket: string
  iceServers: RTCIceServer[]
  sessionId: string
  iceRoute: RealtimeIceRoute
}

export type RealtimeVoiceCallbacks = {
  onTransportStateChanged: (state: string) => void
  onDisconnected: () => void
  onServerMessage: (message: unknown) => void
  onRemoteStream: (stream: MediaStream | null) => void
  onIcePathChanged: (path: RealtimeIcePath | null) => void
  onIceDiagnosticsChanged: (diagnostics: RealtimeIceDiagnostics) => void
}

export type RealtimeIcePath = {
  localType: string
  localProtocol: string
  localRelayProtocol: string | null
  remoteType: string
  remoteProtocol: string
}

export type RealtimeIceDiagnostics = {
  sessionId: string
  route: RealtimeIceRoute
  connectionState: RTCPeerConnectionState
  iceConnectionState: RTCIceConnectionState
  iceGatheringState: RTCIceGatheringState
  iceCandidateErrorCount: number
  lastIceCandidateErrorCode: number | null
  selectedPath: RealtimeIcePath | null
}

export type RealtimeVoiceClient = {
  connect: (connection: RealtimeVoiceConnection) => Promise<void>
  disconnect: () => Promise<void>
  setMicrophoneEnabled: (enabled: boolean) => void
}

export function requestVoiceMicrophone(): Promise<MediaStream> {
  if (!navigator.mediaDevices?.getUserMedia) {
    return Promise.reject(new Error('Microphone capture is unavailable in this browser.'))
  }
  return navigator.mediaDevices.getUserMedia(VOICE_MICROPHONE_CONSTRAINTS)
}

export async function readSelectedIceCandidatePath(
  peer: RTCPeerConnection | null | undefined,
): Promise<RealtimeIcePath | null> {
  if (!peer) return null
  const report = await peer.getStats()
  const entries = [...report.values()] as Array<Record<string, unknown>>
  const transport = entries.find((entry) => entry.type === 'transport')
  const selectedId = transport?.selectedCandidatePairId
  const pair = (typeof selectedId === 'string' ? report.get(selectedId) : undefined)
    ?? entries.find((entry) => entry.type === 'candidate-pair' && entry.selected === true)
  if (!pair || typeof pair.localCandidateId !== 'string' || typeof pair.remoteCandidateId !== 'string') {
    return null
  }
  const local = report.get(pair.localCandidateId) as Record<string, unknown> | undefined
  const remote = report.get(pair.remoteCandidateId) as Record<string, unknown> | undefined
  if (
    local?.type !== 'local-candidate'
    || remote?.type !== 'remote-candidate'
    || typeof local.candidateType !== 'string'
    || typeof local.protocol !== 'string'
    || typeof remote.candidateType !== 'string'
    || typeof remote.protocol !== 'string'
  ) return null
  return {
    localType: local.candidateType,
    localProtocol: local.protocol,
    localRelayProtocol: typeof local.relayProtocol === 'string' ? local.relayProtocol : null,
    remoteType: remote.candidateType,
    remoteProtocol: remote.protocol,
  }
}

export function dispatchVoiceControlMessage(
  message: string,
  onVoiceEvent: (event: unknown) => void,
  onOtherMessage: (message: string) => void,
): boolean {
  try {
    const parsed: unknown = JSON.parse(message)
    if (
      parsed
      && typeof parsed === 'object'
      && !Array.isArray(parsed)
      && 'protocol' in parsed
      && parsed.protocol === 'ai-stack.voice.v1'
      && 'type' in parsed
      && typeof parsed.type === 'string'
    ) {
      onVoiceEvent(parsed)
      return true
    }
  } catch {
    // Pipecat owns parsing and reporting for its standard signaling messages.
  }
  onOtherMessage(message)
  return false
}

function createCapturedStreamMediaManager(stream: MediaStream) {
  const microphoneTrack = stream.getAudioTracks()[0]
  if (!microphoneTrack) throw new Error('The microphone stream has no audio track.')

  let microphoneEnabled = microphoneTrack.enabled
  const setMicrophoneEnabled = (enabled: boolean) => {
    microphoneEnabled = enabled
    microphoneTrack.enabled = enabled
  }
  const mediaManager = {
    supportsScreenShare: false,
    setUserAudioCallback: (_callback: (data: ArrayBuffer) => void) => {},
    setClientOptions: (_options: unknown) => {},
    initialize: async () => {},
    connect: async () => {},
    disconnect: async () => {},
    userStartedSpeaking: async () => {},
    bufferBotAudio: (data: ArrayBuffer | Int16Array) =>
      data instanceof Int16Array ? data : new Int16Array(data),
    getAllMics: async () => [],
    getAllCams: async () => [],
    getAllSpeakers: async () => [],
    updateMic: (_micId: string) => {},
    updateCam: (_camId: string) => {},
    updateSpeaker: (_speakerId: string) => {},
    selectedMic: {},
    selectedCam: {},
    selectedSpeaker: {},
    enableMic: setMicrophoneEnabled,
    enableCam: (_enabled: boolean) => {},
    enableScreenShare: (_enabled: boolean) => {},
    get isCamEnabled() { return false },
    get isMicEnabled() { return microphoneEnabled },
    get isSharingScreen() { return false },
    tracks: () => ({ local: { audio: microphoneTrack }, bot: {} }),
  } as unknown as NonNullable<SmallWebRTCTransportConstructorOptions['mediaManager']>
  return { mediaManager, setMicrophoneEnabled }
}

export function createRealtimeVoiceClient(
  stream: MediaStream,
  callbacks: RealtimeVoiceCallbacks,
): RealtimeVoiceClient {
  const capturedMedia = createCapturedStreamMediaManager(stream)
  const transport = new class extends SmallWebRTCTransport {
    override handleMessage(message: string) {
      dispatchVoiceControlMessage(
        message,
        callbacks.onServerMessage,
        (otherMessage) => super.handleMessage(otherMessage),
      )
    }
  }({
    mediaManager: capturedMedia.mediaManager,
  })
  let candidatePoll: ReturnType<typeof setInterval> | undefined
  let candidateMonitorGeneration = 0
  let peerPoll: ReturnType<typeof setInterval> | undefined
  let monitoredPeer: RTCPeerConnection | null = null
  let peerListeners: Array<[string, EventListener]> = []
  let iceCandidateErrorCount = 0
  let lastIceCandidateErrorCode: number | null = null
  let iceDiagnostics: RealtimeIceDiagnostics | null = null

  const publishIceDiagnostics = () => {
    if (!iceDiagnostics) return
    if (monitoredPeer) {
      iceDiagnostics = {
        ...iceDiagnostics,
        connectionState: monitoredPeer.connectionState,
        iceConnectionState: monitoredPeer.iceConnectionState,
        iceGatheringState: monitoredPeer.iceGatheringState,
        iceCandidateErrorCount,
        lastIceCandidateErrorCode,
      }
    }
    callbacks.onIceDiagnosticsChanged(iceDiagnostics)
  }

  const detachPeer = () => {
    if (monitoredPeer) {
      publishIceDiagnostics()
      for (const [eventName, listener] of peerListeners) {
        monitoredPeer.removeEventListener(eventName, listener)
      }
    }
    peerListeners = []
    monitoredPeer = null
  }

  const attachPeer = () => {
    const peer = (transport as unknown as { pc?: RTCPeerConnection | null }).pc ?? null
    if (peer === monitoredPeer) return
    detachPeer()
    monitoredPeer = peer
    if (!peer) return

    const reportState: EventListener = () => publishIceDiagnostics()
    const reportCandidateError: EventListener = (event) => {
      const code = (event as RTCPeerConnectionIceErrorEvent).errorCode
      iceCandidateErrorCount += 1
      lastIceCandidateErrorCode = Number.isInteger(code) ? code : null
      publishIceDiagnostics()
    }
    for (const eventName of ['connectionstatechange', 'iceconnectionstatechange', 'icegatheringstatechange']) {
      peer.addEventListener(eventName, reportState)
      peerListeners.push([eventName, reportState])
    }
    peer.addEventListener('icecandidateerror', reportCandidateError)
    peerListeners.push(['icecandidateerror', reportCandidateError])
    publishIceDiagnostics()
  }

  const startPeerMonitor = (sessionId: string, route: RealtimeIceRoute) => {
    iceCandidateErrorCount = 0
    lastIceCandidateErrorCode = null
    iceDiagnostics = {
      sessionId,
      route,
      connectionState: 'new',
      iceConnectionState: 'new',
      iceGatheringState: 'new',
      iceCandidateErrorCount: 0,
      lastIceCandidateErrorCode: null,
      selectedPath: null,
    }
    publishIceDiagnostics()
    attachPeer()
    if (peerPoll === undefined) peerPoll = setInterval(attachPeer, 50)
  }

  const stopPeerMonitor = () => {
    if (peerPoll !== undefined) clearInterval(peerPoll)
    peerPoll = undefined
    detachPeer()
  }

  const updateIcePath = async () => {
    const generation = candidateMonitorGeneration
    try {
      const peer = (transport as unknown as { pc?: RTCPeerConnection | null }).pc
      const path = await readSelectedIceCandidatePath(peer)
      if (generation === candidateMonitorGeneration) {
        callbacks.onIcePathChanged(path)
        if (path && iceDiagnostics) {
          iceDiagnostics = { ...iceDiagnostics, selectedPath: path }
          publishIceDiagnostics()
        }
      }
    } catch {
      if (generation === candidateMonitorGeneration) callbacks.onIcePathChanged(null)
    }
  }
  const stopIcePathMonitor = () => {
    candidateMonitorGeneration += 1
    if (candidatePoll !== undefined) clearInterval(candidatePoll)
    candidatePoll = undefined
    callbacks.onIcePathChanged(null)
  }
  const client = new PipecatClient({
    transport,
    enableMic: true,
    enableCam: false,
    callbacks: {
      onTransportStateChanged: (state: string) => {
        attachPeer()
        callbacks.onTransportStateChanged(state)
        if (state === 'connected' || state === 'ready') {
          void updateIcePath()
          if (candidatePoll === undefined) candidatePoll = setInterval(() => void updateIcePath(), 1_000)
        } else if (state === 'disconnected' || state === 'error') {
          stopIcePathMonitor()
          stopPeerMonitor()
        }
      },
      onDisconnected: () => {
        stopIcePathMonitor()
        stopPeerMonitor()
        callbacks.onDisconnected()
      },
      onError: () => {
        stopIcePathMonitor()
        stopPeerMonitor()
        callbacks.onTransportStateChanged('error')
      },
      onServerMessage: callbacks.onServerMessage,
      onTrackStarted: (track, participant) => {
        if (track.kind === 'audio' && participant?.local !== true) {
          callbacks.onRemoteStream(new MediaStream([track]))
        }
      },
      onTrackStopped: (track, participant) => {
        if (track.kind === 'audio' && participant?.local !== true) {
          callbacks.onRemoteStream(null)
        }
      },
    },
  })

  return {
    connect: async ({ offerUrl, ticket, iceServers, sessionId, iceRoute }) => {
      startPeerMonitor(sessionId, iceRoute)
      await client.connect({
        webrtcRequestParams: {
          endpoint: offerUrl,
          headers: new Headers({ 'X-Voice-Session-Ticket': ticket }),
        },
        iceConfig: { iceServers },
      })
      attachPeer()
    },
    disconnect: async () => {
      stopIcePathMonitor()
      stopPeerMonitor()
      await client.disconnect()
    },
    setMicrophoneEnabled: capturedMedia.setMicrophoneEnabled,
  }
}
