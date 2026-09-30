import { PipecatClient } from '@pipecat-ai/client-js'
import {
  SmallWebRTCTransport,
  type SmallWebRTCTransportConstructorOptions,
} from '@pipecat-ai/small-webrtc-transport'

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
}

export type RealtimeVoiceCallbacks = {
  onTransportStateChanged: (state: string) => void
  onDisconnected: () => void
  onServerMessage: (message: unknown) => void
  onRemoteStream: (stream: MediaStream | null) => void
}

export type RealtimeVoiceClient = {
  connect: (connection: RealtimeVoiceConnection) => Promise<void>
  disconnect: () => Promise<void>
}

export function requestVoiceMicrophone(): Promise<MediaStream> {
  if (!navigator.mediaDevices?.getUserMedia) {
    return Promise.reject(new Error('Microphone capture is unavailable in this browser.'))
  }
  return navigator.mediaDevices.getUserMedia(VOICE_MICROPHONE_CONSTRAINTS)
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
  return {
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
    enableMic: (enabled: boolean) => {
      microphoneEnabled = enabled
      microphoneTrack.enabled = enabled
    },
    enableCam: (_enabled: boolean) => {},
    enableScreenShare: (_enabled: boolean) => {},
    get isCamEnabled() { return false },
    get isMicEnabled() { return microphoneEnabled },
    get isSharingScreen() { return false },
    tracks: () => ({ local: { audio: microphoneTrack }, bot: {} }),
  } as unknown as NonNullable<SmallWebRTCTransportConstructorOptions['mediaManager']>
}

export function createRealtimeVoiceClient(
  stream: MediaStream,
  callbacks: RealtimeVoiceCallbacks,
): RealtimeVoiceClient {
  const transport = new class extends SmallWebRTCTransport {
    override handleMessage(message: string) {
      dispatchVoiceControlMessage(
        message,
        callbacks.onServerMessage,
        (otherMessage) => super.handleMessage(otherMessage),
      )
    }
  }({
    mediaManager: createCapturedStreamMediaManager(stream),
  })
  const client = new PipecatClient({
    transport,
    enableMic: true,
    enableCam: false,
    callbacks: {
      onTransportStateChanged: callbacks.onTransportStateChanged,
      onDisconnected: callbacks.onDisconnected,
      onError: () => callbacks.onTransportStateChanged('error'),
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
    connect: async ({ offerUrl, ticket, iceServers }) => {
      await client.connect({
        webrtcRequestParams: {
          endpoint: offerUrl,
          headers: new Headers({ 'X-Voice-Session-Ticket': ticket }),
        },
        iceConfig: { iceServers },
      })
    },
    disconnect: () => client.disconnect(),
  }
}
