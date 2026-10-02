import { useEffect, useRef, useState } from 'react'
import { CircleStop, Mic, RefreshCw, Volume2 } from 'lucide-react'
import { localAI } from './api'
import './SpeechPanel.css'

function prettyError(error: unknown) {
  return error instanceof Error ? error.message : String(error)
}

export function SpeechPanel() {
  const [ttsText, setTtsText] = useState(
    'Hello. This is Local AI running entirely on Marcus Computer.'
  )
  const [audioUrl, setAudioUrl] = useState('')
  const [transcript, setTranscript] = useState('')
  const [busy, setBusy] = useState<'tts' | 'stt' | ''>('')
  const [error, setError] = useState('')
  const [capture, setCapture] = useState<'idle' | 'requesting' | 'recording' | 'stopping'>('idle')
  const recorderRef = useRef<MediaRecorder | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const mountedRef = useRef(true)
  const captureActiveRef = useRef(false)
  const recordingSupported = typeof MediaRecorder !== 'undefined' && !!navigator.mediaDevices?.getUserMedia
  const occupied = busy !== '' || capture !== 'idle'

  function releaseMicrophone() {
    streamRef.current?.getTracks().forEach((track) => track.stop())
    streamRef.current = null
  }

  function discardRecording() {
    const recorder = recorderRef.current
    recorderRef.current = null
    if (recorder) {
      recorder.ondataavailable = null
      recorder.onstop = null
      recorder.onerror = null
      try {
        if (recorder.state !== 'inactive') recorder.stop()
      } catch {
        // A failed recorder must still release its microphone and detached handlers.
      } finally {
        releaseMicrophone()
      }
    } else releaseMicrophone()
    captureActiveRef.current = false
  }

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      discardRecording()
    }
  }, [])

  useEffect(() => () => {
    if (audioUrl) URL.revokeObjectURL(audioUrl)
  }, [audioUrl])

  async function startRecording() {
    if (!recordingSupported || occupied || captureActiveRef.current) return
    captureActiveRef.current = true
    setCapture('requesting')
    setError('')
    setTranscript('')
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      if (!mountedRef.current) {
        stream.getTracks().forEach((track) => track.stop())
        return
      }
      streamRef.current = stream
      const mimeType = ['audio/webm;codecs=opus', 'audio/mp4', 'audio/ogg;codecs=opus']
        .find((type) => MediaRecorder.isTypeSupported?.(type))
      const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined)
      recorderRef.current = recorder
      const chunks: Blob[] = []
      recorder.ondataavailable = (event) => {
        if (event.data.size) chunks.push(event.data)
      }
      recorder.onerror = () => {
        discardRecording()
        if (mountedRef.current) {
          setCapture('idle')
          setError('Audio recording failed. Please try again or choose an audio file.')
        }
      }
      recorder.onstop = () => {
        recorderRef.current = null
        releaseMicrophone()
        captureActiveRef.current = false
        if (!mountedRef.current) return
        setCapture('idle')
        const type = recorder.mimeType || chunks[0]?.type || 'audio/webm'
        const blob = new Blob(chunks, { type })
        if (!blob.size) {
          setError('No audio was captured. Please try again.')
          return
        }
        const extension = type.includes('mp4') ? 'm4a' : type.includes('ogg') ? 'ogg' : 'webm'
        void transcribe(new File([blob], `recording.${extension}`, { type }))
      }
      recorder.start()
      setCapture('recording')
    } catch (err) {
      discardRecording()
      if (mountedRef.current) {
        setCapture('idle')
        setError(prettyError(err))
      }
    }
  }

  function stopRecording() {
    const recorder = recorderRef.current
    if (!recorder || capture !== 'recording') return
    setCapture('stopping')
    try {
      recorder.stop()
    } catch (err) {
      discardRecording()
      setCapture('idle')
      setError(prettyError(err))
    }
  }

  async function speak() {
    setBusy('tts')
    setError('')
    try {
      const blob = await localAI.speak(ttsText)
      const nextAudioUrl = URL.createObjectURL(blob)
      if (!mountedRef.current) {
        URL.revokeObjectURL(nextAudioUrl)
        return
      }
      setAudioUrl(nextAudioUrl)
    } catch (err) {
      if (mountedRef.current) setError(prettyError(err))
    } finally {
      if (mountedRef.current) setBusy('')
    }
  }

  async function transcribe(file?: File) {
    if (!file) return
    setBusy('stt')
    setError('')
    try {
      const result = await localAI.transcribe(file)
      if (mountedRef.current) setTranscript(result.text)
    } catch (err) {
      if (mountedRef.current) setError(prettyError(err))
    } finally {
      if (mountedRef.current) setBusy('')
    }
  }

  return (
    <section className="workspace">
      <div className="workspace-head">
        <div><span className="eyebrow">AUDIO LAB</span><h2>Listen and speak locally.</h2></div>
      </div>
      <div className="split-grid">
        <div className="tool-card">
          <div className="tool-title">
            <Volume2 size={20} />
            <div><h3>Text to speech</h3><p>Qwen3-TTS · GPU scheduled</p></div>
          </div>
          <textarea value={ttsText} onChange={(e) => setTtsText(e.target.value)} />
          <button className="primary" onClick={speak} disabled={occupied || !ttsText.trim()}>
            {busy === 'tts' ? <RefreshCw className="spin" size={16} /> : <Volume2 size={16} />}
            Generate voice
          </button>
          {audioUrl && <audio className="audio-player" controls src={audioUrl} autoPlay />}
        </div>
        <div className="tool-card">
          <div className="tool-title">
            <Mic size={20} />
            <div><h3>Speech to text</h3><p>Whisper large-v3 · VAD enabled</p></div>
          </div>
          <div className={'speech-record-control' + (capture === 'recording' ? ' is-recording' : '')}>
            <button
              type="button"
              className="speech-record-button"
              onClick={capture === 'recording' ? stopRecording : startRecording}
              disabled={!recordingSupported || (occupied && capture !== 'recording')}
              aria-describedby="speech-record-status"
            >
              <span className="speech-record-icon" aria-hidden="true">
                {capture === 'recording' ? <CircleStop size={24} /> :
                  capture === 'requesting' || capture === 'stopping' || busy === 'stt'
                    ? <RefreshCw className="spin" size={24} /> : <Mic size={24} />}
              </span>
              <span>{capture === 'recording' ? 'Stop and transcribe' : capture === 'requesting' ? 'Opening microphone…' :
                capture === 'stopping' ? 'Finishing recording…' : busy === 'stt' ? 'Transcribing…' : 'Record audio'}</span>
            </button>
            <p id="speech-record-status" role="status">
              {!recordingSupported ? 'Microphone recording is unavailable. Use a supported browser on HTTPS or localhost, or choose an audio file.' :
                capture === 'recording' ? 'Recording · tap stop when you’re finished.' :
                  capture === 'requesting' ? 'Allow microphone access to begin.' :
                    busy === 'stt' ? 'Your audio is being transcribed locally.' : 'Tap to record. Stop to get your transcript.'}
            </p>
          </div>
          <label className="drop-zone">
            <Mic size={26} />
            <strong>{busy === 'stt' ? 'Transcribing…' : 'Drop or choose audio'}</strong>
            <span>WAV, MP3, M4A, OGG and most common containers</span>
            <input
              type="file"
              accept="audio/*"
              disabled={occupied}
              aria-label="Choose audio file"
              onChange={(e) => transcribe(e.target.files?.[0])}
            />
          </label>
          {transcript && <div className="result-box" aria-live="polite">{transcript}</div>}
        </div>
      </div>
      {error && <div className="error-banner" role="alert">{error}</div>}
    </section>
  )
}
