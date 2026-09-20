import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import {
  Activity, AudioLines, Bot, BrainCircuit, ChevronRight, CircleStop,
  Cpu, Gauge, Image as ImageIcon, MessageSquareText, Mic, Network, Play,
  RefreshCw, Sparkles, Video, Volume2, Wrench,
} from 'lucide-react'
import { localAI } from './api'
import type { ModelInfo, NetworkStatus, Snapshot, SupervisorStatus } from './api'
import {
  ControlOverview, HealthPanel, JobsPanel, ModelsPanel, NetworkPanel, SetupPanel, StateBadge,
} from './OpsPanels'
import { isBadState } from './state'
import './index.css'

type Section = 'overview' | 'models' | 'jobs' | 'health' | 'chat' | 'speech' | 'vision' | 'studio' | 'setup' | 'network' | 'system'
type ChatMessage = { role: 'user' | 'assistant'; content: string }

const navGroups = [
  {
    label: 'Monitor',
    items: [
      ['overview', Activity, 'Overview'],
      ['models', BrainCircuit, 'Models'],
      ['jobs', Gauge, 'Jobs'],
      ['health', Cpu, 'Health'],
    ],
  },
  {
    label: 'Work',
    items: [
      ['chat', MessageSquareText, 'Chat'],
      ['speech', AudioLines, 'Speech'],
      ['vision', Bot, 'Robot vision'],
      ['studio', Sparkles, 'Studio'],
    ],
  },
  {
    label: 'Configure',
    items: [
      ['setup', Wrench, 'Setup'],
      ['network', Network, 'Network'],
      ['system', Wrench, 'API'],
    ],
  },
] as const

const sectionMeta: Record<Section, { title: string; context: string }> = {
  overview: { title: 'Overview', context: 'Machine and scheduler' },
  models: { title: 'Models', context: 'Fleet and runtime configuration' },
  jobs: { title: 'Jobs', context: 'Recent inference activity' },
  health: { title: 'Health', context: 'Control plane and diagnostics' },
  chat: { title: 'Chat', context: 'Local language models' },
  speech: { title: 'Speech', context: 'Transcription and voice' },
  vision: { title: 'Robot vision', context: 'Visual reasoning' },
  studio: { title: 'Studio', context: 'Image and video generation' },
  setup: { title: 'Setup', context: 'Workstation configuration' },
  network: { title: 'Network', context: 'Remote access and exposure' },
  system: { title: 'API', context: 'Local interface and control' },
}

function prettyError(error: unknown) {
  return error instanceof Error ? error.message : String(error)
}

function StatusDot({ status }: { status: string }) {
  const active = status === 'running'
  return <span className={'status-dot ' + (active ? 'online' : 'offline')} />
}

function GlobalStatusStrip(props: {
  snapshot: Snapshot | null
  network: NetworkStatus | null
  gatewayOk: boolean
  models: ModelInfo[]
  nowSeconds: number
}) {
  const serviceStates = Object.values(props.snapshot?.supervisor.service_states || {})
  const unhealthy = serviceStates.some((state) => isBadState(state))
  const job = props.snapshot?.jobs.active[0]
  const owner = props.snapshot?.supervisor.gpu_owner
  const ownerIds: Record<string, string> = {
    llm: 'local-fast', reasoning: 'local-reasoning', stt: 'local-stt', tts: 'local-tts',
    vlm: 'local-vlm', comfyui: 'local-image', wangp: 'local-video',
  }
  const ownerModel = owner ? props.models.find((m) => m.id === ownerIds[owner]) : undefined
  const ownerLabel = ownerModel ? String(ownerModel.metadata.display_name || ownerModel.id) : owner
  const ageSeconds = props.snapshot && props.nowSeconds
    ? Math.max(0, props.nowSeconds - props.snapshot.timestamp)
    : Number.POSITIVE_INFINITY
  const telemetryState = !props.snapshot || ageSeconds > 60 ? 'error' : ageSeconds > 15 ? 'warn' : 'ready'
  const mcpMode = props.network?.mcp_mode || 'off'
  const warnings = [
    unhealthy ? 'worker' : '',
    telemetryState === 'warn' ? 'stale telemetry' : '',
    mcpMode === 'public' ? 'public MCP' : '',
    props.network?.legacy_443 ? 'legacy route' : '',
  ].filter(Boolean)
  const systemState = !props.gatewayOk || telemetryState === 'error' ? 'error' : warnings.length ? 'warn' : 'ready'
  const systemLabel = systemState === 'ready'
    ? 'HEALTHY'
    : systemState === 'error'
      ? 'ERROR'
      : `${warnings.length} WARNING${warnings.length === 1 ? '' : 'S'}`
  return (
    <div className="global-status-strip">
      <div className="status-summary"><span>SYSTEM</span><strong>{systemLabel}</strong><StateBadge state={systemState} /></div>
      <div><span>GPU</span><strong>{ownerLabel || 'IDLE'}</strong></div>
      <div><span>JOB</span><strong>{job ? job.phase.replaceAll('_', ' ') : 'IDLE'}</strong></div>
      <div><span>TELEMETRY</span><StateBadge state={telemetryState} /><small>{Number.isFinite(ageSeconds) ? Math.round(ageSeconds) + 's old' : 'no sample'}</small></div>
      <div><span>MQTT</span><StateBadge state={props.snapshot?.mqtt.connected ? 'ready' : 'stopped'} /></div>
      <div><span>TAILSCALE</span><StateBadge state={props.network?.online ? 'ready' : 'stopped'} /></div>
      <div><span>MCP</span><strong className={mcpMode === 'public' ? 'public-exposure' : ''}>
        {props.network ? mcpMode.toUpperCase() : 'UNKNOWN'}
      </strong></div>
    </div>
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
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null)
  const [models, setModels] = useState<ModelInfo[]>([])
  const [network, setNetwork] = useState<NetworkStatus | null>(null)
  const [busyServices, setBusyServices] = useState<Set<string>>(new Set())
  const [error, setError] = useState('')
  const [gatewayOk, setGatewayOk] = useState(false)
  const [nowSeconds, setNowSeconds] = useState(0)

  useEffect(() => {
    const updateClock = () => setNowSeconds(Date.now() / 1000)
    updateClock()
    const timer = window.setInterval(updateClock, 1000)
    return () => window.clearInterval(timer)
  }, [])

  const applySnapshot = useCallback((next: Snapshot) => {
    setSnapshot(next)
    setStatus(next.supervisor)
    setGatewayOk(next.gateway.status === 'online')
  }, [])

  const refresh = useCallback(async (includeModels = false) => {
    try {
      const [nextSnapshot, health] = await Promise.all([
        localAI.snapshot(),
        localAI.health(),
      ])
      applySnapshot(nextSnapshot)
      setGatewayOk(health.status === 'ok')
      setError('')
      if (includeModels) setModels((await localAI.models()).data)
    } catch (err) {
      setGatewayOk(false)
      setError(prettyError(err))
    }
  }, [applySnapshot])

  useEffect(() => {
    const initial = window.setTimeout(() => void refresh(true), 0)
    const timer = window.setInterval(() => void refresh(false), 10000)
    return () => {
      window.clearTimeout(initial)
      window.clearInterval(timer)
    }
  }, [refresh])

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const next = await localAI.network()
        if (alive) setNetwork(next)
      } catch {
        if (alive) setNetwork(null)
      }
    }
    void load()
    const timer = window.setInterval(() => void load(), 15000)
    return () => { alive = false; window.clearInterval(timer) }
  }, [])

  useEffect(() => {
    let socket: WebSocket | null = null
    let retry = 0
    let closed = false
    const connect = () => {
      if (closed) return
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      socket = new WebSocket(`${protocol}//${window.location.host}/api/events`)
      socket.onmessage = (event) => {
        try {
          applySnapshot(JSON.parse(event.data) as Snapshot)
          setError('')
        } catch {
          // Fallback polling will recover malformed frames.
        }
      }
      socket.onopen = () => setGatewayOk(true)
      socket.onclose = () => {
        if (!closed) retry = window.setTimeout(connect, 2000)
      }
    }
    connect()
    return () => {
      closed = true
      window.clearTimeout(retry)
      socket?.close()
    }
  }, [applySnapshot])

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

  const content = section === 'models' ? <ModelsPanel models={models} snapshot={snapshot} /> :
    section === 'jobs' ? <JobsPanel snapshot={snapshot} /> :
    section === 'health' ? <HealthPanel snapshot={snapshot} /> :
    section === 'chat' ? <ChatPanel models={models} /> :
    section === 'speech' ? <SpeechPanel /> :
    section === 'vision' ? <VisionPanel /> :
    section === 'studio' ? (
      <StudioPanel onStart={(name) => serviceAction(name, 'start')} busy={busyServices} />
    ) :
    section === 'setup' ? <SetupPanel snapshot={snapshot} models={models} network={network}
      onNavigate={(next) => setSection(next)} /> :
    section === 'network' ? <NetworkPanel network={network} onNetwork={setNetwork} /> :
    section === 'system' ? (
      <SystemPanel models={models} status={status} onStopAll={stopAll} />
    ) : null

  const meta = sectionMeta[section]

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark"><BrainCircuit size={19} /></div>
          <div><strong>Local AI</strong><span>Marcus Computer</span></div>
        </div>

        <nav className="side-nav">
          {navGroups.map((group) => (
            <div className="nav-group" key={group.label}>
              <div className="nav-group-label">{group.label}</div>
              {group.items.map(([key, Icon, label]) => (
                <button
                  key={key}
                  className={section === key ? 'active' : ''}
                  onClick={() => setSection(key)}
                >
                  <Icon size={17} strokeWidth={1.8} />
                  <span>{label}</span>
                </button>
              ))}
            </div>
          ))}
        </nav>

        <div className="sidebar-foot">
          <div className="machine-state">
            <StatusDot status={gatewayOk ? 'running' : 'offline'} />
            <div>
              <strong>Marcus Computer</strong>
              <span>{gatewayOk ? 'Control plane online' : 'Control plane unavailable'}</span>
            </div>
          </div>
          <div className="gpu-pill">
            <Cpu size={15} strokeWidth={1.8} />
            <span>{snapshot?.machine.gpu?.name || 'RTX 3060'}</span>
            <b>{((snapshot?.machine.gpu?.vram_total_mib || 12288) / 1024).toFixed(0)} GB</b>
          </div>
        </div>
      </aside>

      <main className="main-shell">
        <header className="app-topbar">
          <div className="topbar-context">
            <div className="topbar-path">
              <span>Local AI</span><ChevronRight size={13} /><strong>{meta.title}</strong>
            </div>
            <p>{meta.context}</p>
          </div>
          <div className="topbar-state">
            <div className="topbar-chip">
              <StatusDot status={gatewayOk ? 'running' : 'offline'} />
              <span>{gatewayOk ? 'Gateway online' : 'Gateway offline'}</span>
            </div>
            <div className="topbar-chip">
              <Cpu size={14} />
              <span>{snapshot?.machine.gpu ? Math.round(snapshot.machine.gpu.utilization_percent) + '% GPU' : 'GPU —'}</span>
            </div>
          </div>
        </header>

        <div className="main-content">
          <GlobalStatusStrip snapshot={snapshot} network={network} gatewayOk={gatewayOk} models={models} nowSeconds={nowSeconds} />
          {section === 'overview' ? (
            <ControlOverview
              snapshot={snapshot}
              models={models}
              busy={busyServices}
              onStart={(name) => serviceAction(name, 'start')}
              onStop={(name) => serviceAction(name, 'stop')}
              onRefresh={() => void refresh(true)}
            />
          ) : content}
        </div>
        {error && <div className="global-error error-banner">{error}</div>}
      </main>
    </div>
  )
}
