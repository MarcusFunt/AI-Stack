import { useCallback, useEffect, useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import {
  Activity, AudioLines, Bot, BrainCircuit, ChevronRight, CircleStop,
  Cpu, Gauge, Image as ImageIcon, MessageSquareText, Mic, Play,
  RefreshCw, Rocket, Sparkles, Square, Video, Volume2, Wrench,
} from 'lucide-react'
import { localAI } from './api'
import type { ModelInfo, SupervisorStatus } from './api'
import './index.css'

type Section = 'overview' | 'chat' | 'speech' | 'vision' | 'studio' | 'system'
type ChatMessage = { role: 'user' | 'assistant'; content: string }

const nav = [
  ['overview', Activity, 'Overview'],
  ['chat', MessageSquareText, 'Chat'],
  ['speech', AudioLines, 'Speech'],
  ['vision', Bot, 'Robot vision'],
  ['studio', Sparkles, 'Studio'],
  ['system', Wrench, 'System'],
] as const

const serviceMeta: Record<string, { label: string; detail: string; icon: typeof Cpu }> = {
  llm: { label: 'Fast LLM', detail: 'Qwen3.5-9B · ~53 tok/s', icon: Rocket },
  reasoning: { label: 'Reasoning', detail: 'Qwen3.8-27B · deep mode', icon: BrainCircuit },
  stt: { label: 'Speech to text', detail: 'faster-whisper large-v3', icon: Mic },
  tts: { label: 'Text to speech', detail: 'Qwen3-TTS', icon: Volume2 },
  vlm: { label: 'Robot vision', detail: 'Qwen3-VL-4B', icon: Bot },
  comfyui: { label: 'Image studio', detail: 'ComfyUI', icon: ImageIcon },
  wangp: { label: 'Video studio', detail: 'WanGP', icon: Video },
  lerobot: { label: 'LeRobot', detail: 'robot policy workspace', icon: Cpu },
}

function prettyError(error: unknown) {
  return error instanceof Error ? error.message : String(error)
}

function StatusDot({ status }: { status: string }) {
  const active = status === 'running'
  return <span className={'status-dot ' + (active ? 'online' : 'offline')} />
}

function MetricCard(props: { label: string; value: string; sub: string; icon: typeof Cpu }) {
  const Icon = props.icon
  return (
    <div className="metric-card">
      <div className="metric-icon"><Icon size={18} /></div>
      <div>
        <span>{props.label}</span>
        <strong>{props.value}</strong>
        <small>{props.sub}</small>
      </div>
    </div>
  )
}

function ServiceCard(props: {
  name: string
  status: string
  jobs: number
  idle?: number
  busy: boolean
  onStart: () => void
  onStop: () => void
}) {
  const meta = serviceMeta[props.name] || { label: props.name, detail: 'local service', icon: Cpu }
  const Icon = meta.icon
  const running = props.status === 'running'
  return (
    <article className={'service-card ' + (running ? 'is-running' : '')}>
      <div className="service-top">
        <div className="service-icon"><Icon size={19} /></div>
        <div className="service-copy">
          <div className="service-title-row">
            <h3>{meta.label}</h3><StatusDot status={props.status} />
          </div>
          <p>{meta.detail}</p>
        </div>
      </div>
      <div className="service-bottom">
        <div className="service-state">
          <span>{running ? 'ACTIVE' : props.status.toUpperCase()}</span>
          {props.jobs > 0 && <b>{props.jobs} job{props.jobs === 1 ? '' : 's'}</b>}
          {props.idle !== undefined && props.idle > 0 && <b>sleep in {Math.ceil(props.idle)}s</b>}
        </div>
        <button
          className={running ? 'icon-button danger' : 'icon-button'}
          disabled={props.busy || props.jobs > 0}
          onClick={running ? props.onStop : props.onStart}
          title={running ? 'Stop service' : 'Load service'}
        >
          {props.busy ? <RefreshCw className="spin" size={15} /> :
            running ? <Square size={14} /> : <Play size={14} />}
        </button>
      </div>
    </article>
  )
}

function ChatPanel({ models }: { models: ModelInfo[] }) {
  const chatModels = models.filter((m) => m.capabilities.includes('chat'))
  const [model, setModel] = useState('local-fast')
  const [input, setInput] = useState('')
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function send(event: FormEvent) {
    event.preventDefault()
    const text = input.trim()
    if (!text || busy) return
    const next = [...messages, { role: 'user' as const, content: text }]
    setMessages(next)
    setInput('')
    setBusy(true)
    setError('')
    try {
      const result = await localAI.chat(model, next)
      const content = result.choices?.[0]?.message?.content || '(empty response)'
      setMessages([...next, { role: 'assistant', content }])
    } catch (err) {
      setError(prettyError(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="workspace">
      <div className="workspace-head">
        <div><span className="eyebrow">LLM PLAYGROUND</span><h2>One chat, two gears.</h2></div>
        <select value={model} onChange={(e) => setModel(e.target.value)}>
          {chatModels.map((m) => (
            <option key={m.id} value={m.id}>{String(m.metadata.display_name || m.id)}</option>
          ))}
        </select>
      </div>

      <div className="chat-window">
        {messages.length === 0 && (
          <div className="empty-state">
            <BrainCircuit size={34} />
            <h3>Ask Local AI anything</h3>
            <p>Choose Fast for interactive work or Reasoning for difficult coding and analysis. GPU ownership switches automatically.</p>
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} className={'message ' + m.role}>
            <span>{m.role === 'user' ? 'YOU' : 'LOCAL AI'}</span>
            <div>{m.content}</div>
          </div>
        ))}
        {busy && (
          <div className="message assistant pending">
            <span>LOCAL AI</span>
            <div><RefreshCw className="spin" size={16} /> Loading model / thinking…</div>
          </div>
        )}
      </div>
      {error && <div className="error-banner">{error}</div>}
      <form className="composer" onSubmit={send}>
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Message Local AI…"
        />
        <button disabled={busy || !input.trim()}><ChevronRight size={18} /></button>
      </form>
    </section>
  )
}

function SpeechPanel() {
  const [ttsText, setTtsText] = useState(
    'Hello. This is Local AI running entirely on Marcus Computer.'
  )
  const [audioUrl, setAudioUrl] = useState('')
  const [transcript, setTranscript] = useState('')
  const [busy, setBusy] = useState<'tts' | 'stt' | ''>('')
  const [error, setError] = useState('')

  async function speak() {
    setBusy('tts')
    setError('')
    try {
      const blob = await localAI.speak(ttsText)
      if (audioUrl) URL.revokeObjectURL(audioUrl)
      setAudioUrl(URL.createObjectURL(blob))
    } catch (err) {
      setError(prettyError(err))
    } finally {
      setBusy('')
    }
  }

  async function transcribe(file?: File) {
    if (!file) return
    setBusy('stt')
    setError('')
    try {
      const result = await localAI.transcribe(file)
      setTranscript(result.text)
    } catch (err) {
      setError(prettyError(err))
    } finally {
      setBusy('')
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
          <button className="primary" onClick={speak} disabled={busy !== '' || !ttsText.trim()}>
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
          <label className="drop-zone">
            <Mic size={26} />
            <strong>{busy === 'stt' ? 'Transcribing…' : 'Drop or choose audio'}</strong>
            <span>WAV, MP3, M4A, OGG and most common containers</span>
            <input
              type="file"
              accept="audio/*"
              disabled={busy !== ''}
              onChange={(e) => transcribe(e.target.files?.[0])}
            />
          </label>
          {transcript && <div className="result-box">{transcript}</div>}
        </div>
      </div>
      {error && <div className="error-banner">{error}</div>}
    </section>
  )
}

function VisionPanel() {
  const [file, setFile] = useState<File | null>(null)
  const [preview, setPreview] = useState('')
  const [prompt, setPrompt] = useState(
    'Describe this scene for a robot. Identify objects, obstacles, traversable areas, geometry, hazards, and useful affordances.'
  )
  const [result, setResult] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  function selectFile(next?: File) {
    if (!next) return
    if (preview) URL.revokeObjectURL(preview)
    setFile(next)
    setPreview(URL.createObjectURL(next))
    setResult('')
  }

  async function analyze() {
    if (!file) return
    setBusy(true)
    setError('')
    try {
      setResult((await localAI.analyzeVision(file, prompt)).text)
    } catch (err) {
      setError(prettyError(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="workspace">
      <div className="workspace-head">
        <div><span className="eyebrow">ROBOT PERCEPTION</span><h2>See the world as a robot.</h2></div>
      </div>
      <div className="vision-grid">
        <label className={'vision-preview ' + (preview ? 'has-image' : '')}>
          {preview ? <img src={preview} alt="Vision input" /> : (
            <>
              <Bot size={36} />
              <strong>Select a camera frame</strong>
              <span>PNG, JPEG or WebP</span>
            </>
          )}
          <input type="file" accept="image/*" onChange={(e) => selectFile(e.target.files?.[0])} />
        </label>
        <div className="vision-controls">
          <label>ROBOT PROMPT</label>
          <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} />
          <button className="primary" disabled={!file || busy} onClick={analyze}>
            {busy ? <RefreshCw className="spin" size={16} /> : <Bot size={16} />}
            Analyze scene
          </button>
          {result && <div className="result-box tall">{result}</div>}
        </div>
      </div>
      {error && <div className="error-banner">{error}</div>}
    </section>
  )
}

function StudioPanel(props: { onStart: (name: string) => void; busy: Set<string> }) {
  const studios = [
    {
      name: 'comfyui',
      title: 'Image Studio',
      desc: 'ComfyUI workflows for generation, editing, ControlNet and upscaling.',
      href: 'http://127.0.0.1:8188',
      icon: ImageIcon,
    },
    {
      name: 'wangp',
      title: 'Video Studio',
      desc: 'WanGP for long-running image-to-video and text-to-video jobs.',
      href: 'http://127.0.0.1:7860',
      icon: Video,
    },
  ]

  return (
    <section className="workspace">
      <div className="workspace-head">
        <div><span className="eyebrow">GENERATION STUDIO</span><h2>Images and video, same machine.</h2></div>
      </div>
      <div className="studio-grid">
        {studios.map((studio) => {
          const Icon = studio.icon
          return (
            <div className="studio-card" key={studio.name}>
              <div className="studio-art">
                <Icon size={42} />
                <div className="glow-orb" />
              </div>
              <span className="eyebrow">{studio.name.toUpperCase()}</span>
              <h3>{studio.title}</h3>
              <p>{studio.desc}</p>
              <div className="studio-actions">
                <button
                  className="secondary"
                  disabled={props.busy.has(studio.name)}
                  onClick={() => props.onStart(studio.name)}
                >
                  {props.busy.has(studio.name) ? <RefreshCw className="spin" size={15} /> : <Play size={15} />}
                  Load on GPU
                </button>
                <a className="primary link-button" href={studio.href} target="_blank" rel="noreferrer">
                  Open studio <ChevronRight size={15} />
                </a>
              </div>
            </div>
          )
        })}
      </div>
      <div className="info-strip">
        <Gauge size={18} />
        <span>
          Loading either studio automatically evicts the current heavyweight GPU service.
          Active requests are allowed to finish first.
        </span>
      </div>
    </section>
  )
}

function SystemPanel(props: {
  models: ModelInfo[]
  status: SupervisorStatus | null
  onStopAll: () => void
}) {
  return (
    <section className="workspace">
      <div className="workspace-head">
        <div><span className="eyebrow">CONTROL PLANE</span><h2>System and API.</h2></div>
        <button className="danger-button" onClick={props.onStopAll}>
          <CircleStop size={16} /> Stop all GPU services
        </button>
      </div>
      <div className="tool-card">
        <h3>Model registry</h3>
        <div className="model-table">
          {props.models.map((model) => (
            <div className="model-row" key={model.id}>
              <div>
                <strong>{String(model.metadata.display_name || model.id)}</strong>
                <span>{model.id}</span>
              </div>
              <div className="capabilities">
                {model.capabilities.map((cap) => <b key={cap}>{cap}</b>)}
              </div>
              <span>
                {model.metadata.size_gib ? String(model.metadata.size_gib) + ' GiB' : 'managed service'}
              </span>
            </div>
          ))}
        </div>
      </div>
      <div className="split-grid system-grid">
        <div className="tool-card">
          <h3>Unified API</h3>
          <code>POST /v1/chat/completions</code>
          <code>POST /v1/audio/transcriptions</code>
          <code>POST /v1/audio/speech</code>
          <code>POST /v1/vision/analyze</code>
        </div>
        <div className="tool-card">
          <h3>Supervisor</h3>
          <p>Lease epoch <b>{props.status?.lease_epoch ?? '—'}</b></p>
          <p>GPU owner <b>{props.status?.gpu_owner || 'idle'}</b></p>
          <p>Gateway stays resident; heavyweight workers are demand-loaded and released after idle timeouts.</p>
        </div>
      </div>
    </section>
  )
}

export default function App() {
  const [section, setSection] = useState<Section>('overview')
  const [status, setStatus] = useState<SupervisorStatus | null>(null)
  const [models, setModels] = useState<ModelInfo[]>([])
  const [busyServices, setBusyServices] = useState<Set<string>>(new Set())
  const [error, setError] = useState('')
  const [gatewayOk, setGatewayOk] = useState(false)

  const refresh = useCallback(async (includeModels = false) => {
    try {
      const [nextStatus, health] = await Promise.all([
        localAI.status(),
        localAI.health(),
      ])
      setStatus(nextStatus)
      setGatewayOk(health.status === 'ok')
      setError('')
      if (includeModels) setModels((await localAI.models()).data)
    } catch (err) {
      setGatewayOk(false)
      setError(prettyError(err))
    }
  }, [])

  useEffect(() => {
    const initial = window.setTimeout(() => void refresh(true), 0)
    const timer = window.setInterval(() => void refresh(false), 2500)
    return () => {
      window.clearTimeout(initial)
      window.clearInterval(timer)
    }
  }, [refresh])

  async function serviceAction(name: string, action: 'start' | 'stop') {
    setBusyServices((old) => new Set(old).add(name))
    setError('')
    try {
      if (action === 'start') await localAI.start(name)
      else await localAI.stop(name)
      await refresh(false)
    } catch (err) {
      setError(prettyError(err))
    } finally {
      setBusyServices((old) => {
        const next = new Set(old)
        next.delete(name)
        return next
      })
    }
  }

  async function stopAll() {
    try {
      await localAI.stopAll()
      await refresh(false)
    } catch (err) {
      setError(prettyError(err))
    }
  }

  const runningCount = status?.running_gpu_services.length || 0
  const activeJobs = useMemo(
    () => Object.values(status?.active_jobs || {}).reduce((sum, count) => sum + count, 0),
    [status],
  )

  const content = section === 'chat' ? <ChatPanel models={models} /> :
    section === 'speech' ? <SpeechPanel /> :
    section === 'vision' ? <VisionPanel /> :
    section === 'studio' ? (
      <StudioPanel onStart={(name) => serviceAction(name, 'start')} busy={busyServices} />
    ) :
    section === 'system' ? (
      <SystemPanel models={models} status={status} onStopAll={stopAll} />
    ) : null

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark"><Sparkles size={18} /></div>
          <div><strong>LOCAL AI</strong><span>MARCUS COMPUTER</span></div>
        </div>
        <nav>
          {nav.map(([key, Icon, label]) => (
            <button
              key={key}
              className={section === key ? 'active' : ''}
              onClick={() => setSection(key)}
            >
              <Icon size={17} /><span>{label}</span>
            </button>
          ))}
        </nav>
        <div className="sidebar-foot">
          <div className="machine-state">
            <StatusDot status={gatewayOk ? 'running' : 'offline'} />
            <div>
              <strong>{gatewayOk ? 'Gateway online' : 'Gateway unavailable'}</strong>
              <span>localhost:3000</span>
            </div>
          </div>
          <div className="gpu-pill"><Cpu size={15} /><span>RTX 3060 · 12 GB</span></div>
        </div>
      </aside>

      <main>
        {section === 'overview' ? (
          <div className="overview">
            <header className="hero">
              <div>
                <span className="eyebrow">LOCAL INFERENCE CONTROL PLANE</span>
                <h1>One machine.<br /><em>Every model.</em></h1>
                <p>
                  Chat, reasoning, speech, robot perception, image generation and video
                  behind one scheduler-aware control surface.
                </p>
              </div>
              <button className="refresh-button" onClick={() => refresh(true)}>
                <RefreshCw size={16} /> Refresh
              </button>
            </header>

            <div className="metrics">
              <MetricCard icon={Cpu} label="GPU OWNER" value={status?.gpu_owner || 'Idle'} sub={runningCount ? 'exclusive GPU lease' : '12 GB VRAM available'} />
              <MetricCard icon={Activity} label="ACTIVE JOBS" value={String(activeJobs)} sub={activeJobs ? 'requests in flight' : 'scheduler is clear'} />
              <MetricCard icon={BrainCircuit} label="REGISTERED" value={String(models.length)} sub="logical AI services" />
              <MetricCard icon={Gauge} label="GATEWAY" value={gatewayOk ? 'Online' : 'Offline'} sub="unified authenticated API" />
            </div>

            <div className="section-title">
              <div><span className="eyebrow">GPU SCHEDULER</span><h2>Services</h2></div>
              <span>Only one heavyweight service owns the GPU at a time.</span>
            </div>
            <div className="service-grid">
              {Object.entries(status?.services || {}).map(([name, state]) => (
                <ServiceCard
                  key={name}
                  name={name}
                  status={state}
                  jobs={status?.active_jobs[name] || 0}
                  idle={status?.idle_stop_in_seconds[name]}
                  busy={busyServices.has(name)}
                  onStart={() => serviceAction(name, 'start')}
                  onStop={() => serviceAction(name, 'stop')}
                />
              ))}
            </div>
            {error && <div className="error-banner">{error}</div>}
          </div>
        ) : content}
      </main>
    </div>
  )
}
