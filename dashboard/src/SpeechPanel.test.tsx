import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SpeechPanel } from './SpeechPanel'

const api = vi.hoisted(() => ({ transcribe: vi.fn(), speak: vi.fn() }))
vi.mock('./api', () => ({ localAI: api }))

// Browser capture is external to jsdom; this double emits the same final data/stop events.
class Recorder extends EventTarget {
  static isTypeSupported(type: string) { return type === 'audio/webm;codecs=opus' }
  static latest: Recorder
  state = 'inactive'
  mimeType = 'audio/webm;codecs=opus'
  ondataavailable: ((event: BlobEvent) => void) | null = null
  onstop: (() => void) | null = null
  onerror: (() => void) | null = null
  constructor() { super(); Recorder.latest = this }
  start() { this.state = 'recording' }
  stop() {
    this.state = 'inactive'
    this.ondataavailable?.({ data: new Blob(['spoken audio'], { type: this.mimeType }) } as BlobEvent)
    this.onstop?.()
  }
}

const stopTrack = vi.fn()
const stream = { getTracks: () => [{ stop: stopTrack }] } as unknown as MediaStream
const getUserMedia = vi.fn()

async function startRecording() {
  fireEvent.click(screen.getByRole('button', { name: /record audio/i }))
  await screen.findByRole('button', { name: /stop.*transcribe/i })
}

describe('SpeechPanel microphone recording', () => {
  beforeEach(() => {
    stopTrack.mockReset()
    getUserMedia.mockReset().mockResolvedValue(stream)
    api.transcribe.mockReset().mockResolvedValue({ text: 'A captured sentence.' })
    api.speak.mockReset().mockResolvedValue(new Blob(['tts audio']))
    vi.stubGlobal('MediaRecorder', Recorder)
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia } })
  })
  afterEach(() => vi.unstubAllGlobals())

  it('sends the selected Qwen speaker with the speech request', async () => {
    render(<SpeechPanel />)
    const voice = screen.getByRole('combobox', { name: /voice/i })

    expect(voice).toHaveValue('Aiden')
    expect(voice.querySelectorAll('option')).toHaveLength(9)
    fireEvent.change(voice, { target: { value: 'Ryan' } })
    fireEvent.change(screen.getByRole('textbox', { name: /expressive instructions/i }), {
      target: { value: 'Speak playfully, with a pause before the last sentence.' },
    })
    fireEvent.click(screen.getByRole('button', { name: /generate voice/i }))

    await waitFor(() => expect(api.speak).toHaveBeenCalledWith(
      'Hello. This is Local AI running entirely on Marcus Computer.',
      'Ryan',
      'Speak playfully, with a pause before the last sentence.',
    ))
    expect(screen.getByText(/English speech only/i)).toBeInTheDocument()
  })

  it('records audio and sends a correctly typed File to transcription after stop', async () => {
    render(<SpeechPanel />)
    await startRecording()
    expect(getUserMedia).toHaveBeenCalledWith({ audio: true })
    expect(api.transcribe).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: /generate voice/i })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: /stop.*transcribe/i }))
    expect(await screen.findByText('A captured sentence.')).toBeInTheDocument()
    const file = api.transcribe.mock.calls[0][0] as File
    expect(file).toBeInstanceOf(File)
    expect(file.size).toBeGreaterThan(0)
    expect(file.type).toBe('audio/webm;codecs=opus')
    expect(file.name).toMatch(/\.webm$/)
    expect(stopTrack).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: /record audio/i })).toBeEnabled()
  })

  it('waits for the browser final audio event before releasing the microphone and transcribing', async () => {
    render(<SpeechPanel />)
    await startRecording()
    const recorder = Recorder.latest
    vi.spyOn(recorder, 'stop').mockImplementation(() => { recorder.state = 'inactive' })

    fireEvent.click(screen.getByRole('button', { name: /stop.*transcribe/i }))

    expect(screen.getByRole('button', { name: /finishing recording/i })).toBeDisabled()
    expect(stopTrack).not.toHaveBeenCalled()
    expect(api.transcribe).not.toHaveBeenCalled()
    act(() => {
      recorder.ondataavailable?.({ data: new Blob(['final audio'], { type: recorder.mimeType }) } as BlobEvent)
      recorder.onstop?.()
    })

    expect(await screen.findByText('A captured sentence.')).toBeInTheDocument()
    expect(stopTrack).toHaveBeenCalledOnce()
    expect(api.transcribe).toHaveBeenCalledOnce()
  })

  it('discards a pending final audio event when unmounted while the recorder is stopping', async () => {
    const view = render(<SpeechPanel />)
    await startRecording()
    const recorder = Recorder.latest
    vi.spyOn(recorder, 'stop').mockImplementation(() => { recorder.state = 'inactive' })

    fireEvent.click(screen.getByRole('button', { name: /stop.*transcribe/i }))
    expect(screen.getByRole('button', { name: /finishing recording/i })).toBeDisabled()
    view.unmount()

    expect(stopTrack).toHaveBeenCalledOnce()
    expect(recorder.ondataavailable).toBeNull()
    expect(recorder.onstop).toBeNull()
    expect(api.transcribe).not.toHaveBeenCalled()
  })

  it('preserves audio file upload transcription', async () => {
    const { container } = render(<SpeechPanel />)
    const file = new File(['upload'], 'sample.wav', { type: 'audio/wav' })
    fireEvent.change(container.querySelector('input[type=file]')!, { target: { files: [file] } })
    expect(await screen.findByText('A captured sentence.')).toBeInTheDocument()
    expect(api.transcribe).toHaveBeenCalledWith(file)
  })

  it.each(['recorder', 'microphone'])('explains unsupported %s capture while leaving upload available', (missing) => {
    if (missing === 'recorder') vi.stubGlobal('MediaRecorder', undefined)
    else Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: undefined })
    const { container } = render(<SpeechPanel />)
    expect(screen.getByRole('button', { name: /record audio/i })).toBeDisabled()
    expect(screen.getByText(/recording.*unavailable/i)).toBeInTheDocument()
    expect(container.querySelector('input[type=file]')).toBeEnabled()
  })

  it('shows microphone permission errors and allows retry', async () => {
    getUserMedia.mockRejectedValueOnce(new DOMException('Permission denied', 'NotAllowedError'))
    render(<SpeechPanel />)
    fireEvent.click(screen.getByRole('button', { name: /record audio/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/permission|denied/i)
    expect(screen.getByRole('button', { name: /record audio/i })).toBeEnabled()
    await startRecording()
  })

  it('shows transcription API errors and releases the microphone', async () => {
    api.transcribe.mockRejectedValueOnce(new Error('Transcription worker unavailable'))
    render(<SpeechPanel />)
    await startRecording()
    fireEvent.click(screen.getByRole('button', { name: /stop.*transcribe/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Transcription worker unavailable')
    expect(stopTrack).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: /record audio/i })).toBeEnabled()
  })

  it('stops recording on unmount without submitting audio', async () => {
    const view = render(<SpeechPanel />)
    await startRecording()
    view.unmount()
    expect(Recorder.latest.state).toBe('inactive')
    expect(stopTrack).toHaveBeenCalledTimes(1)
    expect(api.transcribe).not.toHaveBeenCalled()
  })

  it('releases a microphone granted after the page is unmounted', async () => {
    let grant!: (value: MediaStream) => void
    getUserMedia.mockReturnValueOnce(new Promise<MediaStream>((resolve) => { grant = resolve }))
    const view = render(<SpeechPanel />)
    fireEvent.click(screen.getByRole('button', { name: /record audio/i }))
    view.unmount()
    await act(async () => grant(stream))
    expect(stopTrack).toHaveBeenCalledTimes(1)
    expect(api.transcribe).not.toHaveBeenCalled()
  })

  it('releases the microphone on recorder failure and allows retry', async () => {
    render(<SpeechPanel />)
    await startRecording()
    act(() => Recorder.latest.onerror?.())
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(/recording.*failed/i))
    expect(stopTrack).toHaveBeenCalledTimes(1)
    expect(api.transcribe).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: /record audio/i })).toBeEnabled()
  })

  it('shows a stop failure even when recorder cleanup also fails', async () => {
    render(<SpeechPanel />)
    await startRecording()
    vi.spyOn(Recorder.latest, 'stop').mockImplementation(() => { throw new Error('Unable to stop recording') })
    fireEvent.click(screen.getByRole('button', { name: /stop.*transcribe/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Unable to stop recording')
    expect(stopTrack).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: /record audio/i })).toBeEnabled()
    expect(api.transcribe).not.toHaveBeenCalled()
  })

  it('revokes generated speech audio URLs when the page unmounts', async () => {
    const createObjectURL = vi.fn(() => 'blob:generated-speech')
    const revokeObjectURL = vi.fn()
    vi.stubGlobal('URL', { createObjectURL, revokeObjectURL })
    api.speak.mockResolvedValue(new Blob(['audio']))
    const view = render(<SpeechPanel />)
    fireEvent.click(screen.getByRole('button', { name: /generate voice/i }))
    await waitFor(() => expect(containerAudio(view.container)?.getAttribute('src')).toBe('blob:generated-speech'))

    view.unmount()

    expect(revokeObjectURL).toHaveBeenCalledWith('blob:generated-speech')
  })

  it('revokes a speech audio URL returned after the page unmounts', async () => {
    const createObjectURL = vi.fn(() => 'blob:late-speech')
    const revokeObjectURL = vi.fn()
    vi.stubGlobal('URL', { createObjectURL, revokeObjectURL })
    let finishSpeech!: (blob: Blob) => void
    api.speak.mockReturnValueOnce(new Promise((resolve) => { finishSpeech = resolve }))
    const view = render(<SpeechPanel />)
    fireEvent.click(screen.getByRole('button', { name: /generate voice/i }))
    view.unmount()
    await act(async () => finishSpeech(new Blob(['late audio'])))

    expect(createObjectURL).toHaveBeenCalledOnce()
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:late-speech')
  })
})

function containerAudio(container: HTMLElement) {
  return container.querySelector('audio')
}
