import { useEffect, useState } from 'react'
import {
  Activity, AlertTriangle, Box, BrainCircuit, Check, Copy, Cpu, Database, Download,
  Gauge, Globe2, HardDrive, MemoryStick, Play, RefreshCw, Router, Save, Settings2,
  Shield, Square, TestTube2, Timer, Wifi, WifiOff,
} from 'lucide-react'
import { localAI } from './api'
import type {
  DoctorCheck, ModelInfo, ModelManagement, NetworkStatus, RuntimeSettings, Snapshot,
} from './api'
import { isBadState } from './state'

const labels: Record<string, string> = {
  llm: 'Fast LLM',
  reasoning: 'Reasoning LLM',
  stt: 'Speech to text',
  tts: 'Text to speech',
  vlm: 'Robot vision',
  comfyui: 'Image studio',
  wangp: 'Video studio',
  lerobot: 'LeRobot',
}

function fmt(n?: number | null, digits = 1) {
  return n === undefined || n === null || Number.isNaN(n) ? '—' : n.toFixed(digits)
}

function duration(seconds?: number | null) {
  if (seconds === undefined || seconds === null) return '—'
  if (seconds < 60) return seconds.toFixed(seconds < 10 ? 1 : 0) + 's'
  const m = Math.floor(seconds / 60)
  const s = Math.floor(seconds % 60)
  return m + 'm ' + String(s).padStart(2, '0') + 's'
}

function tone(state: string) {
  if (['ready', 'complete', 'pass'].includes(state)) return 'ok'
  if (['running'].includes(state)) return 'live'
  if (['starting', 'loading', 'warming', 'waiting', 'unloading', 'warn'].includes(state)) return 'busy'
  if (isBadState(state)) return 'bad'
  return 'muted'
}

export function StateBadge({ state }: { state: string }) {
  return (
    <span className={'state-badge ' + tone(state)}>
      <span className="state-light" />
      {state.replaceAll('_', ' ').toUpperCase()}
    </span>
  )
}

function GaugeBar(props: {
  label: string
  value: number
  max: number
  display: string
  sub?: string
  icon?: typeof Cpu
}) {
  const Icon = props.icon || Gauge
  const pct = Math.max(0, Math.min(100, props.max ? (props.value / props.max) * 100 : 0))
  return (
    <div className="telemetry-card">
      <div className="telemetry-head">
        <div className="telemetry-title"><Icon size={16} /><span>{props.label}</span></div>
        <strong>{props.display}</strong>
      </div>
      <div className="bar-track"><div className="bar-fill" style={{ width: pct + '%' }} /></div>
      <div className="telemetry-sub"><span>{props.sub || ''}</span><span>{pct.toFixed(0)}%</span></div>
    </div>
  )
}

function FixedHistoryChart(props: {
  title: string
  values: Array<number | null>
  timestamps: number[]
  min: number
  max: number
  current: string
  unit: string
}) {
  const [hoverIndex, setHoverIndex] = useState<number | null>(null)
  const span = Math.max(1, props.max - props.min)
  const points = props.values.map((value, i) => {
    if (value === null || Number.isNaN(value)) return null
    const x = props.values.length <= 1 ? 100 : (i / (props.values.length - 1)) * 100
    const clamped = Math.max(props.min, Math.min(props.max, value))
    const y = 42 - ((clamped - props.min) / span) * 36
    return { x, y, value }
  })
  const segments: string[] = []
  let segment: string[] = []
  for (const point of points) {
    if (!point) {
      if (segment.length) segments.push(segment.join(' '))
      segment = []
      continue
    }
    segment.push(point.x.toFixed(2) + ',' + point.y.toFixed(2))
  }
  if (segment.length) segments.push(segment.join(' '))
  const seen = points.flatMap((point) => point ? [point.value] : [])
  const minSeen = seen.length ? Math.min(...seen) : null
  const maxSeen = seen.length ? Math.max(...seen) : null
  const avgSeen = seen.length ? seen.reduce((sum, value) => sum + value, 0) / seen.length : null
  const formatTime = (timestamp?: number) => timestamp
    ? new Date(timestamp * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
    : '—'
  const hoverPoint = hoverIndex !== null ? points[hoverIndex] : null
  const hoverTime = hoverIndex !== null ? props.timestamps[hoverIndex] : undefined
  const handleMove = (event: React.MouseEvent<SVGSVGElement>) => {
    if (props.values.length <= 1) return
    const bounds = event.currentTarget.getBoundingClientRect()
    const ratio = Math.max(0, Math.min(1, (event.clientX - bounds.left) / Math.max(bounds.width, 1)))
    setHoverIndex(Math.round(ratio * (props.values.length - 1)))
  }
  return (
    <div className="history-chart">
      <div className="history-chart-head">
        <div><span>{props.title}</span><strong>{props.current}</strong></div>
        <small>
          {hoverPoint
            ? `${formatTime(hoverTime)} · ${fmt(hoverPoint.value, 1)}${props.unit}`
            : minSeen === null
              ? 'No samples'
              : `min ${fmt(minSeen, 0)} · avg ${fmt(avgSeen, 0)} · max ${fmt(maxSeen, 0)} ${props.unit}`}
        </small>
      </div>
      <svg viewBox="0 0 100 46" preserveAspectRatio="none" aria-label={props.title + ' history'}
        onMouseMove={handleMove} onMouseLeave={() => setHoverIndex(null)}>
        {[6, 24, 42].map((y) => <line key={y} x1="0" x2="100" y1={y} y2={y} />)}
        {segments.map((line, index) => <polyline key={index} points={line} fill="none" vectorEffect="non-scaling-stroke" />)}
        {hoverPoint && <>
          <line className="history-crosshair" x1={hoverPoint.x} x2={hoverPoint.x} y1="6" y2="42" />
          <circle className="history-hover-dot" cx={hoverPoint.x} cy={hoverPoint.y} r="1.15" />
        </>}
      </svg>
      <div className="history-scale"><span>{props.max}{props.unit}</span><span>{props.min}{props.unit}</span></div>
      <div className="history-time"><span>{formatTime(props.timestamps[0])}</span><span>{formatTime(props.timestamps.at(-1))}</span></div>
    </div>
  )
}

const modelIdsByService: Record<string, string> = {
  llm: 'local-fast',
  reasoning: 'local-reasoning',
  stt: 'local-stt',
  tts: 'local-tts',
  vlm: 'local-vlm',
  comfyui: 'local-image',
  wangp: 'local-video',
}

function serviceModel(service: string | null | undefined, models: ModelInfo[]) {
  const id = service ? modelIdsByService[service] : undefined
  return models.find((model) => model.id === id)
}

function serviceForModel(modelId: string) {
  return Object.entries(modelIdsByService).find(([, id]) => id === modelId)?.[0]
    || modelId.replace('local-', '')
}

function CurrentWork({ snapshot, models }: { snapshot: Snapshot | null; models: ModelInfo[] }) {
  const job = snapshot?.jobs.active[0]
  const owner = snapshot?.supervisor.gpu_owner
  const gpu = snapshot?.machine.gpu
  const lastStart = job ? snapshot?.supervisor.service_metrics?.[job.service]?.last_start_s : undefined
  const startupRemaining = job && job.state === 'waiting' && lastStart
    ? Math.max(0, lastStart - job.elapsed_s)
    : null
  if (!job) {
    const loadedModel = serviceModel(owner, models)
    const loadedName = loadedModel
      ? String(loadedModel.metadata.display_name || loadedModel.id)
      : owner ? (labels[owner] || owner) : null
    const idleSeconds = owner ? snapshot?.supervisor.idle_stop_in_seconds?.[owner] : undefined
    const ownerState = owner ? (snapshot?.supervisor.service_states?.[owner] || 'ready') : 'stopped'
    const ownerReady = ownerState === 'ready'
    return (
      <section className={'current-work idle ' + (owner ? 'has-owner' : '')}>
        <div className={'work-icon ' + (owner ? 'live' : '')}><Activity size={20} /></div>
        <div className="work-main">
          <span className="eyebrow">{owner ? 'GPU RESIDENT MODEL' : 'CURRENT WORK'}</span>
          <h2>{owner ? loadedName + (ownerReady ? ' loaded' : ' · ' + ownerState.replaceAll('_', ' ')) : 'Scheduler idle'}</h2>
          <p>{owner
            ? ownerReady
              ? 'No request is active. The worker is warm and ready for another request.'
              : 'The worker owns the GPU and is transitioning toward readiness.'
            : 'No AI service currently owns the GPU. Physical GPU use can still come from Windows, displays, or other applications.'}</p>
        </div>
        <div className="work-side">
          <StateBadge state={owner ? ownerState : 'stopped'} />
          <strong>{fmt(gpu?.utilization_percent, 0)}% GPU · {fmt((gpu?.vram_used_mib || 0) / 1024, 1)} GiB</strong>
          <span>{owner && idleSeconds !== undefined ? 'auto-unload in ' + duration(idleSeconds) : 'physical GPU allocation'}</span>
        </div>
      </section>
    )
  }
  const m = job.metrics || {}
  return (
    <section className="current-work">
      <div className="work-icon live"><Activity size={20} /></div>
      <div className="work-main">
        <span className="eyebrow">CURRENT WORK</span>
        <div className="work-title-row">
          <h2>{labels[job.service] || job.service}</h2>
          <StateBadge state={job.state} />
        </div>
        <p>{job.phase.replaceAll('_', ' ')} · GPU owner: {owner || 'switching'}</p>
        <div className="work-progress">
          <div className="bar-track"><div className="bar-fill indeterminate" /></div>
        </div>
      </div>
      <div className="work-stats">
        <div><span>Elapsed</span><strong>{duration(job.elapsed_s)}</strong></div>
        <div><span>Generation</span><strong>{m.generation_tps ? fmt(Number(m.generation_tps), 2) + ' tok/s' : job.state === 'running' ? 'measuring' : '—'}</strong></div>
        <div><span>Activity age</span><strong className={job.stalled_suspected ? 'danger-text' : ''}>{duration(job.activity_age_s)}</strong></div>
        <div><span>{startupRemaining !== null ? 'LOAD ETA' : 'ACTIVITY'}</span><strong>{startupRemaining !== null ? '~' + duration(startupRemaining) : (job.progress_units || '—')}</strong></div>
      </div>
    </section>
  )
}

export function ControlOverview(props: {
  snapshot: Snapshot | null
  models: ModelInfo[]
  busy: Set<string>
  onStart: (service: string) => void
  onStop: (service: string) => void
  onRefresh: () => void
}) {
  const [history, setHistory] = useState<Snapshot['machine'][]>([])
  const [historyPersistent, setHistoryPersistent] = useState<boolean | null>(null)
  const [windowSeconds, setWindowSeconds] = useState(120)
  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const result = await localAI.telemetryHistory(windowSeconds, 900)
        if (alive) {
          setHistory(result.samples)
          setHistoryPersistent(result.persistent ?? null)
        }
      } catch { /* live cards still work without history */ }
    }
    void load()
    const timer = window.setInterval(() => void load(), 10000)
    return () => { alive = false; window.clearInterval(timer) }
  }, [windowSeconds])

  const gpu = props.snapshot?.machine.gpu
  const system = props.snapshot?.machine.system
  const status = props.snapshot?.supervisor
  const historyTimes = history.map((s) => s.timestamp)
  const gpuSeries = history.map((s) => s.gpu ? s.gpu.utilization_percent : null)
  const vramSeries = history.map((s) => s.gpu ? s.gpu.vram_used_mib / 1024 : null)
  const tempSeries = history.map((s) => s.gpu ? s.gpu.temperature_c : null)
  const powerSeries = history.map((s) => s.gpu ? s.gpu.power_w : null)
  return (
    <div className="overview">
      <header className="compact-hero">
        <div><span className="eyebrow">LOCAL INFERENCE CONTROL PLANE</span><h1>Machine overview</h1>
          <p>Scheduler state and physical hardware telemetry are deliberately shown separately.</p></div>
        <button className="refresh-button" onClick={props.onRefresh}><RefreshCw size={15} /> Refresh</button>
      </header>

      <CurrentWork snapshot={props.snapshot} models={props.models} />

      <div className="telemetry-grid">
        <GaugeBar icon={Cpu} label="GPU LOAD" value={gpu?.utilization_percent || 0} max={100}
          display={fmt(gpu?.utilization_percent, 0) + '%'} sub={gpu?.name || 'telemetry unavailable'} />
        <GaugeBar icon={MemoryStick} label="VRAM" value={gpu?.vram_used_mib || 0} max={gpu?.vram_total_mib || 12288}
          display={fmt((gpu?.vram_used_mib || 0) / 1024, 1) + ' / ' + fmt((gpu?.vram_total_mib || 0) / 1024, 1) + ' GiB'}
          sub="physical allocation" />
        <GaugeBar icon={MemoryStick} label="SYSTEM RAM" value={system?.ram_used_mib || 0} max={system?.ram_total_mib || 1}
          display={fmt((system?.ram_used_mib || 0) / 1024, 1) + ' / ' + fmt((system?.ram_total_mib || 0) / 1024, 1) + ' GiB'}
          sub="Docker/WSL runtime view" />
        <GaugeBar icon={HardDrive} label="MODEL DRIVE" value={(system?.disk_total_gib || 0) - (system?.disk_free_gib || 0)}
          max={system?.disk_total_gib || 1} display={fmt(system?.disk_free_gib, 0) + ' GiB free'} sub="D: model storage" />
      </div>

      <div className="history-toolbar">
        <div><span className="eyebrow">FIXED-SCALE HISTORY</span><strong>Hardware over time</strong>
          <small>{historyPersistent === true ? '7-day minute history persisted on disk' : historyPersistent === false ? 'Persistent history unavailable' : 'Live history'}</small>
        </div>
        <div className="history-window">
          {[[120, '2m'], [600, '10m'], [3600, '1h'], [86400, '24h'], [604800, '7d']].map(([seconds, label]) => (
            <button key={seconds} className={windowSeconds === seconds ? 'active' : ''}
              onClick={() => setWindowSeconds(Number(seconds))}>{label}</button>
          ))}
        </div>
      </div>
      <div className="history-grid">
        <FixedHistoryChart title="GPU LOAD" values={gpuSeries} timestamps={historyTimes} min={0} max={100}
          current={fmt(gpu?.utilization_percent, 0) + '%'} unit="%" />
        <FixedHistoryChart title="VRAM" values={vramSeries} timestamps={historyTimes} min={0}
          max={(gpu?.vram_total_mib || 12288) / 1024}
          current={fmt((gpu?.vram_used_mib || 0) / 1024, 1) + ' GiB'} unit=" GiB" />
        <FixedHistoryChart title="TEMPERATURE" values={tempSeries} timestamps={historyTimes} min={20} max={90}
          current={fmt(gpu?.temperature_c, 0) + '°C'} unit="°C" />
        <FixedHistoryChart title="POWER" values={powerSeries} timestamps={historyTimes} min={0} max={gpu?.power_limit_w || 170}
          current={fmt(gpu?.power_w, 1) + ' W'} unit=" W" />
      </div>

      <div className="section-title ops-title">
        <div><span className="eyebrow">GPU SCHEDULER</span><h2>Service fleet</h2></div>
        <span>One heavyweight worker owns the GPU; stopped is a normal healthy state.</span>
      </div>
      <div className="fleet-matrix">
        <div className="fleet-row fleet-head">
          <span>SERVICE</span><span>MODEL</span><span>STATE</span><span>GPU</span>
          <span>JOBS</span><span>LOAD</span><span>AUTO STOP</span><span>LAST ERROR</span><span>ACTION</span>
        </div>
        {Object.keys(status?.services || {}).map((name) => {
          const state = status?.service_states?.[name] || status?.services[name] || 'unknown'
          const model = serviceModel(name, props.models)
          const metric = status?.service_metrics?.[name]
          const idle = status?.idle_stop_in_seconds[name]
          const jobs = status?.active_jobs[name] || 0
          const running = ['ready', 'running', 'loading', 'starting', 'warming'].includes(state)
          return (
            <div className="fleet-row" key={'fleet-' + name}>
              <strong>{labels[name] || name}</strong>
              <span>{model ? String(model.metadata.display_name || model.id) : 'managed worker'}</span>
              <StateBadge state={state} />
              <span className={status?.gpu_owner === name ? 'owner-pill' : ''}>
                {status?.gpu_owner === name ? 'OWNER' : '—'}
              </span>
              <b>{jobs}</b>
              <span>{duration(metric?.last_start_s)}</span>
              <span>{idle !== undefined ? duration(idle) : '—'}</span>
              <span className={metric?.last_error ? 'danger-text fleet-error' : 'fleet-error'}
                title={metric?.last_error || undefined}>
                {metric?.last_error || '—'}
              </span>
              <button className={running ? 'fleet-action stop' : 'fleet-action'}
                disabled={props.busy.has(name) || jobs > 0}
                aria-label={(running ? 'Stop ' : 'Load ') + (labels[name] || name)}
                onClick={() => running ? props.onStop(name) : props.onStart(name)}>
                {props.busy.has(name) ? <RefreshCw className="spin" size={14} /> :
                  running ? <><Square size={13} /> Stop</> : <><Play size={13} /> Load</>}
              </button>
            </div>
          )
        })}
      </div>
    </div>
  )
}

export function ModelsPanel({ models, snapshot }: { models: ModelInfo[]; snapshot: Snapshot | null }) {
  const [benching, setBenching] = useState('')
  const [results, setResults] = useState<Record<string, Record<string, number | string | null>>>({})
  const [management, setManagement] = useState<ModelManagement | null>(null)
  const [configBusy, setConfigBusy] = useState('')
  const [manageMessage, setManageMessage] = useState('')
  const [installRepo, setInstallRepo] = useState('')
  const [installFile, setInstallFile] = useState('')
  const [installTarget, setInstallTarget] = useState('llm')
  const [installBusy, setInstallBusy] = useState(false)

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const next = await localAI.modelManagement()
        if (alive) setManagement(next)
      } catch (err) {
        if (alive) setManageMessage(err instanceof Error ? err.message : String(err))
      }
    }
    void load()
    const timer = window.setInterval(() => void load(), 10000)
    return () => { alive = false; window.clearInterval(timer) }
  }, [])

  async function benchmark(service: string) {
    setBenching(service)
    try {
      const result = await localAI.benchmark(service)
      setResults((old) => ({ ...old, [service]: result }))
    } finally {
      setBenching('')
    }
  }

  function setConfig(service: string, key: string, value: string) {
    if (!management) return
    setManagement({
      ...management,
      config: {
        ...management.config,
        [service]: { ...(management.config[service] || {}), [key]: value },
      },
    })
  }

  async function saveConfig(service: string) {
    if (!management?.config[service]) return
    setConfigBusy(service)
    setManageMessage('')
    try {
      const result = await localAI.updateModelConfig({
        service, values: management.config[service], recreate: true,
      })
      setManagement({ ...management, config: result.config })
      setManageMessage(labels[service] + ' configuration saved; worker container recreated.')
    } catch (err) {
      setManageMessage(err instanceof Error ? err.message : String(err))
    } finally {
      setConfigBusy('')
    }
  }

  async function install() {
    if (!installRepo.trim()) return
    setInstallBusy(true)
    setManageMessage('')
    try {
      const job = await localAI.installModel({
        repo: installRepo.trim(), filename: installFile.trim() || undefined, target: installTarget,
      })
      setManageMessage('Download started: ' + job.id)
      setInstallRepo('')
      setInstallFile('')
      setTimeout(async () => {
        try { setManagement(await localAI.modelManagement()) } catch { /* next poll will recover */ }
      }, 1000)
    } catch (err) {
      setManageMessage(err instanceof Error ? err.message : String(err))
    } finally {
      setInstallBusy(false)
    }
  }

  return (
    <section className="workspace">
      <div className="workspace-head">
        <div><span className="eyebrow">MODEL FLEET</span><h2>Installed models and measured performance.</h2></div>
      </div>
      <div className="model-card-grid">
        {models.map((model) => {
          const meta = model.metadata
          const service = serviceForModel(model.id)
          const state = snapshot?.supervisor.service_states?.[service] || 'stopped'
          const generated = Number(meta.benchmark_generated_tps || 0)
          const msToken = generated > 0 ? 1000 / generated : 0
          const vram = Number(meta.benchmark_vram_mib || 0)
          const live = results[service]
          return (
            <article className="model-card" key={model.id}>
              <div className="model-card-head">
                <div><span className="eyebrow">{model.capabilities.join(' · ')}</span>
                  <h3>{String(meta.display_name || model.id)}</h3><code>{model.id}</code></div>
                <StateBadge state={state} />
              </div>
              <div className="model-gauges">
                <div><span>DISK</span><strong>{meta.size_gib ? fmt(Number(meta.size_gib), 2) + ' GiB' : 'managed'}</strong></div>
                <div><span>MEASURED VRAM</span><strong>{vram ? fmt(vram / 1024, 2) + ' GiB' : '—'}</strong></div>
                <div><span>GENERATION</span><strong>{generated ? fmt(generated, 2) + ' tok/s' : '—'}</strong></div>
                <div><span>TOKEN TIME</span><strong>{msToken ? fmt(msToken, 0) + ' ms' : '—'}</strong></div>
                <div><span>PROMPT</span><strong>{meta.benchmark_prompt_tps ? fmt(Number(meta.benchmark_prompt_tps), 1) + ' tok/s' : '—'}</strong></div>
                <div><span>COLD START</span><strong>{duration(snapshot?.supervisor.service_metrics?.[service]?.last_start_s)}</strong></div>
              </div>
              {Number(meta.gpu_layers || 0) > 0 && <div className="model-note">GPU layers: {String(meta.gpu_layers)}</div>}
              {live && <div className="benchmark-result">
                Latest run: {fmt(Number(live.generation_tps), 2)} tok/s · {fmt(Number(live.ms_per_token), 0)} ms/token · acquire {duration(Number(live.acquire_s))}
              </div>}
              {['llm', 'reasoning'].includes(service) && (
                <button className="secondary" disabled={!!benching} onClick={() => void benchmark(service)}>
                  {benching === service ? <RefreshCw className="spin" size={14} /> : <Gauge size={14} />}
                  Run benchmark
                </button>
              )}
            </article>
          )
        })}
      </div>

      <div className="section-title ops-title">
        <div><span className="eyebrow">MODEL CONFIGURATION</span><h2>Runtime settings</h2></div>
        <span>Changes recreate only the selected stopped worker. Active jobs block reconfiguration.</span>
      </div>
      <div className="model-config-grid">
        {Object.entries(management?.config || {}).map(([service, values]) => (
          <div className="tool-card model-config-card" key={service}>
            <div className="setup-card-title"><div><BrainCircuit size={18} /><div>
              <span className="eyebrow">{service.toUpperCase()}</span><h3>{labels[service] || service}</h3>
            </div></div><StateBadge state={snapshot?.supervisor.service_states?.[service] || 'stopped'} /></div>
            <div className="config-field-list">
              {Object.entries(values).map(([key, value]) => {
                const llmFiles = management?.inventory?.llm || []
                const isLlmModel = ['llm', 'reasoning'].includes(service) && key.endsWith('_MODEL') && llmFiles.length > 0
                const isContext = key.endsWith('_CONTEXT')
                const isGpuLayers = key.endsWith('_GPU_LAYERS')
                const isComputeType = key === 'STT_COMPUTE_TYPE'
                const hint = isContext
                  ? 'tokens · keep enough VRAM free for KV cache'
                  : isGpuLayers
                    ? '0 = CPU · 999 = full offload when supported'
                    : isLlmModel
                      ? 'installed GGUF on the model drive'
                      : isComputeType
                        ? 'float16 is fastest on the RTX 3060'
                        : ''
                return (
                  <label key={key}>{key.replaceAll('_', ' ')}
                    {isLlmModel ? (
                      <select value={value} onChange={(e) => setConfig(service, key, e.target.value)}>
                        {!llmFiles.some((file) => file.config_path === value) && <option value={value}>{value}</option>}
                        {llmFiles.map((file) => <option key={file.config_path} value={file.config_path}>
                          {file.name}{file.size_gib ? ' · ' + file.size_gib + ' GiB' : ''}
                        </option>)}
                      </select>
                    ) : isComputeType ? (
                      <select value={value} onChange={(e) => setConfig(service, key, e.target.value)}>
                        {['float16', 'int8_float16', 'int8'].map((choice) => <option key={choice} value={choice}>{choice}</option>)}
                      </select>
                    ) : (
                      <input value={value}
                        type={isContext || isGpuLayers ? 'number' : 'text'}
                        min={isContext ? 512 : isGpuLayers ? 0 : undefined}
                        max={isContext ? 262144 : isGpuLayers ? 999 : undefined}
                        step={isContext ? 512 : isGpuLayers ? 1 : undefined}
                        onChange={(e) => setConfig(service, key, e.target.value)} />
                    )}
                    {hint && <small className="config-hint">{hint}</small>}
                  </label>
                )
              })}
            </div>
            <button className="secondary"
              disabled={!!configBusy || !!snapshot?.supervisor.active_jobs?.[service]
                || (snapshot?.supervisor.service_states?.[service] || 'stopped') !== 'stopped'}
              title={(snapshot?.supervisor.service_states?.[service] || 'stopped') !== 'stopped'
                ? 'Stop/unload this worker before changing its container configuration.'
                : undefined}
              onClick={() => void saveConfig(service)}>
              {configBusy === service ? <RefreshCw className="spin" size={14} /> : <Save size={14} />}
              Save & recreate worker
            </button>
          </div>
        ))}
      </div>

      <div className="tool-card model-installer">
        <div className="setup-card-title"><div><Download size={19} /><div>
          <span className="eyebrow">MODEL INSTALLER</span><h3>Download from Hugging Face</h3>
        </div></div></div>
        <div className="installer-form">
          <label>Repository<input value={installRepo} placeholder="owner/model"
            onChange={(e) => setInstallRepo(e.target.value)} /></label>
          <label>Filename (optional)<input value={installFile} placeholder="e.g. model-Q4_K_M.gguf"
            onChange={(e) => setInstallFile(e.target.value)} /></label>
          <label>Target<select value={installTarget} onChange={(e) => setInstallTarget(e.target.value)}>
            <option value="llm">LLM / GGUF</option><option value="stt">Speech to text</option>
            <option value="vlm">Vision</option><option value="tts">Text to speech</option>
            <option value="image">Image</option><option value="video">Video</option>
          </select></label>
          <button className="primary no-margin"
            disabled={installBusy || !installRepo.trim() || management?.installer_available === false}
            onClick={() => void install()}>
            {installBusy ? <RefreshCw className="spin" size={14} /> : <Download size={14} />} Download
          </button>
        </div>
        <p>Downloads run on the Windows host into the existing model folders. Selecting a downloaded model is a separate configuration step above.</p>
        <div className="installer-status">
          <StateBadge state={management?.installer_available === false ? 'error' : 'ready'} />
          <span>{management?.installer_available === false
            ? 'Hugging Face CLI is not available on the host.'
            : 'Hugging Face CLI ready'}</span>
        </div>
        <div className="install-list">
          {(management?.installs || []).map((job) => <div key={job.id}>
            <div><strong>{job.repo}{job.filename ? ' · ' + job.filename : ''}</strong>
              <span>{job.target} · {job.elapsed_s !== undefined ? duration(job.elapsed_s) : job.id}</span></div>
            <StateBadge state={job.state} />
            <div className="install-progress">
              {job.progress_percent !== null && job.progress_percent !== undefined ? <>
                <div className="bar-track"><div className="bar-fill" style={{ width: Math.max(0, Math.min(100, job.progress_percent)) + '%' }} /></div>
                <span>{fmt(job.progress_percent, 0)}%{job.progress_text ? ' · ' + job.progress_text : ''}</span>
              </> : <span>{job.state === 'running' ? 'Downloading · waiting for measurable progress…' : job.state}</span>}
              {job.log_tail && <details><summary>Log</summary><pre>{job.log_tail.split('\n').slice(-8).join('\n')}</pre></details>}
            </div>
          </div>)}
        </div>
        {manageMessage && <div className="setup-message">{manageMessage}</div>}
      </div>
    </section>
  )
}

export function JobsPanel({ snapshot }: { snapshot: Snapshot | null }) {
  const jobs = snapshot?.jobs
  const all = [...(jobs?.active || []), ...(jobs?.recent || [])]
  return (
    <section className="workspace">
      <div className="workspace-head">
        <div><span className="eyebrow">REQUEST TIMELINE</span><h2>Jobs</h2></div>
        <StateBadge state={(jobs?.active.length || 0) ? 'running' : 'ready'} />
      </div>
      <div className="job-list">
        {all.length === 0 && <div className="empty-ops"><Timer size={28} /><strong>No jobs yet</strong><span>Requests will appear here with phase, duration and measured throughput.</span></div>}
        {all.map((job) => (
          <div className="job-row" key={job.id}>
            <div className="job-state-col"><StateBadge state={job.state} /><code>{job.id.slice(0, 8)}</code></div>
            <div className="job-main"><strong>{labels[job.service] || job.service}</strong>
              <span>{job.phase.replaceAll('_', ' ')} · {job.path}</span></div>
            <div className="job-metric"><span>ELAPSED</span><b>{duration(job.elapsed_s)}</b></div>
            <div className="job-metric"><span>THROUGHPUT</span><b>{job.metrics.generation_tps ? fmt(Number(job.metrics.generation_tps), 2) + ' tok/s' : '—'}</b></div>
            <div className="job-metric"><span>ACTIVITY AGE</span><b className={job.stalled_suspected ? 'danger-text' : ''}>{duration(job.activity_age_s)}</b></div>
          </div>
        ))}
      </div>
    </section>
  )
}

function SelfTestPanel({ snapshot }: { snapshot: Snapshot | null }) {
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const tests = snapshot?.self_tests
  const services = ['llm', 'reasoning', 'stt', 'tts', 'vlm', 'comfyui', 'wangp']
  const run = async (service?: string) => {
    setBusy(service || 'all')
    setError('')
    try {
      if (service) await localAI.runSelfTest(service)
      else await localAI.runAllSelfTests()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy('')
    }
  }
  const active = tests?.run.state === 'running'
  const runStarted = tests?.run.started_at || 0
  const currentResults = services.filter((service) => {
    const result = tests?.results?.[service]
    return !!result?.started_at && result.started_at >= runStarted
  })
  const completed = currentResults.filter((service) => !!tests?.results?.[service]?.finished_at)
  const currentService = active
    ? services.find((service) => !completed.includes(service)) || tests?.run.services.at(-1)
    : undefined
  const progress = active ? Math.round((completed.length / services.length) * 100) : (tests?.run.state === 'pass' ? 100 : 0)
  const fullElapsed = tests?.run.started_at && tests.run.finished_at
    ? tests.run.finished_at - tests.run.started_at
    : tests?.run.started_at && snapshot?.timestamp ? snapshot.timestamp - tests.run.started_at : null
  const lastFinished = tests?.run.finished_at
    ? new Date(tests.run.finished_at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : null

  return (
    <div className="tool-card self-test-panel">
      <div className="setup-card-title">
        <div><TestTube2 size={19} /><div><span className="eyebrow">FUNCTIONAL DIAGNOSTICS</span>
          <h3>Service verification</h3></div></div>
        <button className="primary no-margin" disabled={active || !!busy} onClick={() => void run()}>
          {active || busy === 'all' ? <RefreshCw className="spin" size={14} /> : <TestTube2 size={14} />}
          Test entire stack
        </button>
      </div>

      {(active || (tests?.run.state && tests.run.state !== 'idle')) && (
        <div className="self-test-summary">
          <div className="self-test-summary-copy">
            <div>
              <span className="eyebrow">{active ? 'FULL TEST IN PROGRESS' : 'LAST FULL TEST'}</span>
              <strong>{active
                ? `${completed.length} / ${services.length} complete${currentService ? ' · now ' + (labels[currentService] || currentService) : ''}`
                : `${String(tests?.run.state || 'unknown').toUpperCase()}${lastFinished ? ' · ' + lastFinished : ''}`}</strong>
            </div>
            <span>{fullElapsed !== null ? duration(fullElapsed) + (active ? ' elapsed' : ' total') : '—'}</span>
          </div>
          <div className="bar-track self-test-progress"><div className="bar-fill" style={{ width: progress + '%' }} /></div>
        </div>
      )}

      <div className="self-test-grid">
        {services.map((service) => {
          const result = tests?.results?.[service]
          const state = result?.state || 'untested'
          const testDepth = ['comfyui', 'wangp'].includes(service) ? 'Connectivity check' : 'Inference E2E'
          return (
            <div className={'self-test-row ' + (currentService === service ? 'is-current' : '')} key={service}>
              <div><strong>{labels[service] || service}</strong>
                <span><b className="test-depth">{testDepth}</b>{result?.detail ? ' · ' + result.detail : ' · No test has been run yet.'}</span></div>
              <span>{result?.elapsed_s ? duration(result.elapsed_s) : '—'}</span>
              <StateBadge state={currentService === service ? 'running' : state} />
              <button className="secondary no-margin" disabled={active || !!busy}
                onClick={() => void run(service)}>
                {busy === service ? <RefreshCw className="spin" size={13} /> : <Play size={13} />} Test
              </button>
            </div>
          )
        })}
      </div>
      {error && <div className="error-banner">{error}</div>}
    </div>
  )
}

export function HealthPanel({ snapshot }: { snapshot: Snapshot | null }) {
  const supervisor = snapshot?.supervisor
  const machine = snapshot?.machine
  const unhealthy = Object.entries(supervisor?.service_states || {}).filter(([, state]) => isBadState(state))
  const gatewayState = snapshot?.gateway.status === 'online' ? 'ready' : 'error'
  const telemetryState = machine?.gpu ? 'ready' : 'error'
  return (
    <section className="workspace">
      <div className="workspace-head"><div><span className="eyebrow">HEALTH & TOPOLOGY</span><h2>Control plane</h2></div></div>
      <div className="topology">
        <div className="topology-node"><Router size={19} /><strong>Dashboard</strong><StateBadge state="ready" /></div>
        <span>→</span>
        <div className="topology-node"><Wifi size={19} /><strong>Gateway</strong><StateBadge state={gatewayState} /></div>
        <span>→</span>
        <div className="topology-node"><Settings2 size={19} /><strong>Supervisor</strong><StateBadge state={supervisor ? 'ready' : 'error'} /></div>
        <span>→</span>
        <div className="topology-node"><Gauge size={19} /><strong>Telemetry</strong><StateBadge state={telemetryState} /></div>
      </div>
      <div className="health-grid">
        <div className="health-card"><Cpu size={20} /><div><span>GPU</span><strong>{machine?.gpu?.name || 'Unavailable'}</strong>
          <small>{fmt(machine?.gpu?.temperature_c, 0)}°C · {fmt(machine?.gpu?.power_w, 1)} W</small></div><StateBadge state={machine?.gpu ? 'ready' : 'error'} /></div>
        <div className="health-card"><Box size={20} /><div><span>GPU WORKERS</span><strong>{Object.keys(supervisor?.services || {}).length} configured</strong>
          <small>{unhealthy.length ? unhealthy.length + ' need attention' : 'stopped workers are normal'}</small></div><StateBadge state={unhealthy.length ? 'warn' : 'ready'} /></div>
        <div className="health-card"><Activity size={20} /><div><span>SUPERVISOR HEARTBEAT</span><strong>{supervisor?.heartbeat && snapshot?.timestamp ? duration(snapshot.timestamp - supervisor.heartbeat) + ' ago' : '—'}</strong>
          <small>lease epoch {supervisor?.lease_epoch ?? '—'}</small></div><StateBadge state={supervisor ? 'ready' : 'error'} /></div>
        <div className="health-card">{snapshot?.mqtt.connected ? <Wifi size={20} /> : <WifiOff size={20} />}<div><span>MQTT</span>
          <strong>{snapshot?.mqtt.connected ? 'Connected' : 'Not connected'}</strong><small>{snapshot?.mqtt.last_error || 'optional telemetry output'}</small></div>
          <StateBadge state={snapshot?.mqtt.connected ? 'ready' : 'stopped'} /></div>
      </div>
      <div className="section-title ops-title"><div><span className="eyebrow">WORKERS</span><h2>Semantic states</h2></div></div>
      <div className="health-worker-list">
        {Object.entries(supervisor?.service_states || {}).map(([name, state]) => (
          <div key={name}><strong>{labels[name] || name}</strong><span>{supervisor?.services[name]}</span><StateBadge state={state} /></div>
        ))}
      </div>
      <SelfTestPanel snapshot={snapshot} />
    </section>
  )
}

function CheckRow({ check }: { check: DoctorCheck }) {
  const Icon = check.status === 'pass' ? Check : AlertTriangle
  return (
    <div className={'check-row check-' + check.status}>
      <div className="check-icon"><Icon size={16} /></div>
      <div><strong>{check.label}</strong><span>{check.detail}</span></div>
      <StateBadge state={check.status} />
    </div>
  )
}

export function SetupPanel(props: {
  snapshot: Snapshot | null
  models: ModelInfo[]
  network: NetworkStatus | null
  onNavigate: (section: 'network' | 'models' | 'health') => void
}) {
  const { snapshot, models, network } = props
  const [checks, setChecks] = useState<DoctorCheck[]>([])
  const [doctorState, setDoctorState] = useState('loading')
  const [settings, setSettings] = useState<RuntimeSettings | null>(null)
  const [password, setPassword] = useState('')
  const [saving, setSaving] = useState(false)
  const [mqttTesting, setMqttTesting] = useState(false)
  const [message, setMessage] = useState('')

  async function refreshDoctor() {
    setDoctorState('loading')
    try {
      const result = await localAI.doctor()
      setChecks(result.checks)
      setDoctorState(result.status)
    } catch {
      setDoctorState('fail')
    }
  }

  useEffect(() => {
    let cancelled = false
    async function loadInitial() {
      try {
        const [result, currentSettings] = await Promise.all([
          localAI.doctor(),
          localAI.settings(),
        ])
        if (!cancelled) {
          setChecks(result.checks)
          setDoctorState(result.status)
          setSettings(currentSettings)
        }
      } catch {
        if (!cancelled) setDoctorState('fail')
      }
    }
    void loadInitial()
    return () => { cancelled = true }
  }, [])

  async function testMqtt() {
    setMqttTesting(true)
    setMessage('')
    try {
      const result = await localAI.mqttTest()
      setMessage('MQTT broker acknowledged a test publish on ' + result.topic)
    } catch (err) {
      setMessage(err instanceof Error ? err.message : String(err))
    } finally {
      setMqttTesting(false)
    }
  }

  async function save() {
    if (!settings) return
    setSaving(true)
    setMessage('')
    try {
      const payload: RuntimeSettings = {
        mqtt: { ...settings.mqtt },
      }
      if (password) payload.mqtt.password = password
      else delete payload.mqtt.password
      const result = await localAI.updateSettings(payload)
      setSettings(result)
      setPassword('')
      setMessage('Settings saved. MQTT reconnects automatically within a few seconds.')
    } catch (err) {
      setMessage(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  const mqtt = settings?.mqtt
  const apiReady = snapshot?.gateway.status === 'online'
  const networkAvailable = !!network?.online && !!network?.dashboard_enabled
  const networkSecure = networkAvailable && network?.mcp_mode !== 'public' && !network?.legacy_443
  const modelReady = models.length > 0 && models.every((model) => {
    const state = snapshot?.supervisor.service_states?.[serviceForModel(model.id)]
    return !isBadState(state)
  })
  const smokeReady = snapshot?.self_tests?.run.state === 'pass'
  const wizardSteps = [
    { id: 'host', title: 'Host, Docker & GPU', detail: 'Prerequisites and persistent storage', state: doctorState === 'pass' ? 'ready' : doctorState === 'loading' ? 'starting' : 'error', action: () => void refreshDoctor(), actionLabel: 'Run checks' },
    { id: 'api', title: 'Control plane API', detail: 'Gateway, supervisor and telemetry', state: apiReady ? 'ready' : 'error', action: () => props.onNavigate('health'), actionLabel: 'Open health' },
    { id: 'network', title: 'Remote access', detail: !networkAvailable ? 'Tailscale dashboard needs configuration' : networkSecure ? 'Private dashboard and non-public MCP' : 'Online · review public/legacy exposure', state: networkSecure ? 'ready' : 'warn', action: () => props.onNavigate('network'), actionLabel: 'Configure' },
    { id: 'models', title: 'Model fleet', detail: `${models.length} registered capabilities`, state: modelReady ? 'ready' : 'warn', action: () => props.onNavigate('models'), actionLabel: 'Review models' },
    { id: 'smoke', title: 'Functional smoke test', detail: smokeReady ? 'Latest full-stack run passed' : 'Run inference/connectivity verification', state: smokeReady ? 'ready' : 'warn', action: () => props.onNavigate('health'), actionLabel: 'Run tests' },
  ]
  return (
    <section className="workspace">
      <div className="workspace-head">
        <div><span className="eyebrow">SETUP & INTEGRATIONS</span><h2>Configure without editing files.</h2></div>
        <button className="secondary no-margin" onClick={() => void refreshDoctor()}>
          <RefreshCw className={doctorState === 'loading' ? 'spin' : ''} size={14} /> Run checks
        </button>
      </div>

      <div className="setup-wizard">
        <div className="setup-wizard-head">
          <div><span className="eyebrow">FIRST-RUN PATH</span><h3>Bring the workstation to a verified ready state</h3></div>
          <span>{wizardSteps.filter((step) => step.state === 'ready').length} / {wizardSteps.length} required steps ready</span>
        </div>
        <div className="wizard-step-grid">
          {wizardSteps.map((step, index) => <div className="wizard-step" key={step.id}>
            <div className="wizard-number">{index + 1}</div>
            <div className="wizard-copy"><strong>{step.title}</strong><span>{step.detail}</span></div>
            <StateBadge state={step.state} />
            <button className="secondary no-margin" onClick={step.action}>{step.actionLabel}</button>
          </div>)}
        </div>
        <div className="wizard-optional">
          <span>OPTIONAL INTEGRATION</span>
          <strong>MQTT / Home Assistant</strong>
          <StateBadge state={snapshot?.mqtt.connected ? 'ready' : mqtt?.enabled ? 'waiting' : 'stopped'} />
          <small>{snapshot?.mqtt.connected ? 'Connected and publishing telemetry.' : 'Configure below if you want external telemetry.'}</small>
        </div>
      </div>

      <div className="setup-grid">
        <div className="tool-card setup-card">
          <div className="setup-card-title"><div><Database size={19} /><div><span className="eyebrow">SYSTEM DOCTOR</span><h3>Prerequisites</h3></div></div>
            <StateBadge state={doctorState === 'loading' ? 'starting' : doctorState} /></div>
          <div className="check-list">{checks.map((c) => <CheckRow key={c.id} check={c} />)}</div>
          {!checks.length && <div className="setup-loading"><RefreshCw className="spin" size={18} /> Checking Docker, GPU and storage…</div>}
        </div>

        <div className="tool-card setup-card">
          <div className="setup-card-title"><div>{snapshot?.mqtt.connected ? <Wifi size={19} /> : <WifiOff size={19} />}<div>
            <span className="eyebrow">MQTT TELEMETRY</span><h3>Home Assistant / ESP32</h3></div></div>
            <StateBadge state={snapshot?.mqtt.connected ? 'ready' : mqtt?.enabled ? 'waiting' : 'stopped'} /></div>
          {mqtt && <>
            <label className="switch-row"><input type="checkbox" checked={mqtt.enabled}
              onChange={(e) => setSettings({ ...settings!, mqtt: { ...mqtt, enabled: e.target.checked } })} />
              <span><strong>Enable MQTT output</strong><small>Read-only telemetry; no command topics are subscribed.</small></span></label>
            <div className="form-grid">
              <label>Broker host<input value={mqtt.host} placeholder="192.168.1.10"
                onChange={(e) => setSettings({ ...settings!, mqtt: { ...mqtt, host: e.target.value } })} /></label>
              <label>Port<input type="number" value={mqtt.port}
                onChange={(e) => setSettings({ ...settings!, mqtt: { ...mqtt, port: Number(e.target.value) } })} /></label>
              <label>Username<input value={mqtt.username}
                onChange={(e) => setSettings({ ...settings!, mqtt: { ...mqtt, username: e.target.value } })} /></label>
              <label>Password<input type="password" value={password}
                placeholder={mqtt.password_configured ? 'Configured — leave blank to keep' : 'Optional'}
                onChange={(e) => setPassword(e.target.value)} /></label>
            </div>

            <label className="switch-row"><input type="checkbox" checked={mqtt.home_assistant_discovery}
              onChange={(e) => setSettings({ ...settings!, mqtt: { ...mqtt, home_assistant_discovery: e.target.checked } })} />
              <span><strong>Home Assistant discovery</strong><small>Publishes retained discovery entities automatically.</small></span></label>
            <div className="form-grid">
              <label>Discovery prefix<input value={mqtt.discovery_prefix}
                onChange={(e) => setSettings({ ...settings!, mqtt: { ...mqtt, discovery_prefix: e.target.value } })} /></label>
              <label>Publish every<input type="number" min="1" max="60" step="1" value={mqtt.publish_interval}
                onChange={(e) => setSettings({ ...settings!, mqtt: { ...mqtt, publish_interval: Number(e.target.value) } })} /><span className="input-unit">seconds</span></label>
            </div>
            <div className="setup-actions">
              <button className="primary setup-save" disabled={saving} onClick={() => void save()}>
                {saving ? <RefreshCw className="spin" size={14} /> : <Check size={14} />} Save & connect
              </button>
              <button className="secondary setup-save" disabled={mqttTesting || !snapshot?.mqtt.connected}
                onClick={() => void testMqtt()}>
                {mqttTesting ? <RefreshCw className="spin" size={14} /> : <TestTube2 size={14} />} Test broker
              </button>
            </div>
            {message && <div className="setup-message">{message}</div>}
            {snapshot?.mqtt.last_error && <div className="error-banner">{snapshot.mqtt.last_error}</div>}
          </>}
        </div>
      </div>

      <div className="tool-card setup-models">
        <div className="setup-card-title"><div><BrainCircuit size={19} /><div><span className="eyebrow">MODEL READINESS</span><h3>{models.length} logical AI capabilities registered</h3></div></div></div>
        <div className="setup-model-list">
          {models.map((model) => {
            const service = serviceForModel(model.id)
            const raw = snapshot?.supervisor.services?.[service] || 'unknown'
            const state = snapshot?.supervisor.service_states?.[service] || (raw === 'not-created' ? 'error' : 'stopped')
            return <div key={model.id}>
              <div><strong>{String(model.metadata.display_name || model.id)}</strong><span>{model.id} · {model.capabilities.join(', ')}</span></div>
              <StateBadge state={state} />
            </div>
          })}
        </div>
        <p>Stopped is normal: heavyweight workers are demand-loaded. “Not created” or error states need attention.</p>
      </div>

      <div className="tool-card mqtt-topics">
        <div className="setup-card-title"><div><Router size={19} /><div><span className="eyebrow">OUTPUT CONTRACT</span><h3>MQTT topics</h3></div></div></div>
        <div className="topic-grid">
          <code>localai/marcus-computer/status</code>
          <code>localai/marcus-computer/gpu/utilization</code>
          <code>localai/marcus-computer/gpu/vram_used_mb</code>
          <code>localai/marcus-computer/gpu/temperature_c</code>
          <code>localai/marcus-computer/scheduler/owner</code>
          <code>localai/marcus-computer/service/&lt;name&gt;/state</code>
        </div>
        <p>These are retained telemetry outputs. MQTT control is intentionally disabled by design.</p>
      </div>
    </section>
  )
}

export function NetworkPanel(props: {
  network: NetworkStatus | null
  onNetwork: (status: NetworkStatus | null) => void
}) {
  const [dashboardEnabled, setDashboardEnabled] = useState(true)
  const [mcpMode, setMcpMode] = useState<'public' | 'private' | 'off'>('public')
  const [clearLegacy, setClearLegacy] = useState(false)
  const [initialized, setInitialized] = useState(false)
  const [busy, setBusy] = useState('')
  const [message, setMessage] = useState('')

  const effectiveDashboardEnabled = initialized
    ? dashboardEnabled
    : (props.network?.dashboard_enabled ?? !!props.network?.dashboard_url)
  const effectiveMcpMode: 'public' | 'private' | 'off' = initialized
    ? mcpMode
    : (props.network?.mcp_mode || (props.network?.mcp_url ? 'private' : 'off'))

  const refresh = async () => {
    setBusy('refresh')
    setMessage('')
    try {
      const next = await localAI.network()
      props.onNetwork(next)
      setInitialized(false)
    } catch (err) {
      setMessage(err instanceof Error ? err.message : String(err))
      props.onNetwork(null)
    } finally {
      setBusy('')
    }
  }

  const apply = async () => {
    if (effectiveMcpMode === 'public' && props.network?.mcp_mode !== 'public') {
      const confirmed = window.confirm(
        'Public Funnel makes the authenticated MCP endpoint reachable from the public internet. Continue?'
      )
      if (!confirmed) return
    }
    setBusy('apply')
    setMessage('')
    try {
      const result = await localAI.configureTailscale({
        dashboard_enabled: effectiveDashboardEnabled, mcp_mode: effectiveMcpMode, clear_legacy_443: clearLegacy,
      })
      props.onNetwork(result.status)
      setInitialized(false)
      setClearLegacy(false)
      setMessage(result.ok ? 'Tailscale routes updated and verified.' : 'One or more Tailscale commands failed.')
    } catch (err) {
      setMessage(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy('')
    }
  }

  const applySecureDefaults = async () => {
    setBusy('secure')
    setMessage('')
    try {
      const result = await localAI.configureTailscale({
        dashboard_enabled: true,
        mcp_mode: 'private',
        clear_legacy_443: true,
      })
      props.onNetwork(result.status)
      setDashboardEnabled(true)
      setMcpMode('private')
      setInitialized(false)
      setClearLegacy(false)
      setMessage(result.ok
        ? 'Secure defaults applied: private dashboard, tailnet-only MCP, legacy :443 removed.'
        : 'One or more secure-default commands failed.')
    } catch (err) {
      setMessage(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy('')
    }
  }

  const copy = async (value?: string | null) => {
    if (!value) return
    await navigator.clipboard.writeText(value)
    setMessage('Copied to clipboard.')
  }

  const serve = props.network?.serve_status || ''
  const publicMcp = props.network?.mcp_mode !== undefined
    ? props.network.mcp_mode === 'public'
    : (serve.includes('Funnel on') && serve.includes(':10000'))
  const legacy443 = props.network?.legacy_443
    ?? (serve.includes(':443') && serve.includes('127.0.0.1:8000'))
  const routes = [
    ['Dashboard local', 'http://127.0.0.1:3000/'],
    ['Unified API local', 'http://127.0.0.1:8090/v1/'],
    ['Dashboard private', props.network?.dashboard_url || 'Unavailable'],
    ['MCP endpoint', props.network?.mcp_url || 'Unavailable'],
  ]
  const routeCards = [
    {
      port: ':8443',
      title: 'Dashboard',
      target: '127.0.0.1:3000',
      exposure: props.network?.dashboard_enabled ? 'TAILNET ONLY' : 'OFF',
      state: props.network?.dashboard_enabled ? 'ready' : 'stopped',
    },
    {
      port: ':10000',
      title: 'MCP',
      target: '127.0.0.1:8765',
      exposure: publicMcp ? 'PUBLIC FUNNEL' : props.network?.mcp_mode === 'private' ? 'TAILNET ONLY' : 'OFF',
      state: publicMcp ? 'warn' : props.network?.mcp_mode === 'private' ? 'ready' : 'stopped',
    },
    {
      port: ':443',
      title: 'Legacy API route',
      target: '127.0.0.1:8000',
      exposure: legacy443 ? 'TAILNET ONLY · LEGACY' : 'OFF',
      state: legacy443 ? 'warn' : 'stopped',
    },
  ]

  return (
    <section className="workspace">
      <div className="workspace-head">
        <div><span className="eyebrow">NETWORK & API</span><h2>Remote access and exposure.</h2></div>
        <button className="secondary no-margin" onClick={() => void refresh()} disabled={!!busy}>
          <RefreshCw className={busy === 'refresh' ? 'spin' : ''} size={14} /> Refresh actual state
        </button>
      </div>
      <div className="network-summary-grid">
        <div className="health-card"><Globe2 size={20} /><div><span>TAILSCALE</span>
          <strong>{props.network?.online ? 'Online' : 'Unavailable'}</strong>
          <small>{props.network?.dns_name || 'Host agent has not reported a DNS name'}</small></div>
          <StateBadge state={props.network?.online ? 'ready' : 'error'} /></div>
        <div className="health-card"><Router size={20} /><div><span>REMOTE DASHBOARD</span>
          <strong>{props.network?.dashboard_enabled ? 'Tailnet :8443' : 'Disabled'}</strong>
          <small>{props.network?.tailscale_ips?.[0] || 'No Tailscale IP reported'}</small></div>
          <StateBadge state={props.network?.dashboard_enabled ? 'ready' : 'stopped'} /></div>
        <div className="health-card"><Shield size={20} /><div><span>MCP EXPOSURE</span>
          <strong className={publicMcp ? 'danger-text' : ''}>{publicMcp ? 'Public Funnel' : props.network?.mcp_mode === 'private' ? 'Tailnet only' : 'Off'}</strong>
          <small>{publicMcp ? 'Internet reachable; MCP authentication still required.' : 'No public Funnel detected.'}</small></div>
          <StateBadge state={publicMcp ? 'warn' : props.network?.mcp_mode === 'private' ? 'ready' : 'stopped'} /></div>
        <div className="health-card"><Settings2 size={20} /><div><span>HOST BRIDGE</span>
          <strong>{props.network ? 'Connected' : 'Unavailable'}</strong>
          <small>{props.network?.host_agent_version ? 'Windows agent v' + props.network.host_agent_version : 'Gateway → Windows integration'}</small></div>
          <StateBadge state={props.network ? 'ready' : 'error'} /></div>
      </div>

      <div className="setup-grid network-grid">
        <div className="tool-card">
          <div className="setup-card-title"><div><Globe2 size={19} /><div>
            <span className="eyebrow">TAILSCALE ROUTES</span><h3>Configure from the GUI</h3></div></div></div>
          <label className="switch-row"><input type="checkbox" checked={effectiveDashboardEnabled}
            onChange={(e) => { setDashboardEnabled(e.target.checked); setInitialized(true) }} />
            <span><strong>Private dashboard on :8443</strong>
              <small>Accessible only to devices in your tailnet.</small></span></label>
          <div className="form-grid single-control">
            <label>MCP exposure<select value={effectiveMcpMode}
              onChange={(e) => { setMcpMode(e.target.value as 'public' | 'private' | 'off'); setInitialized(true) }}>
              <option value="public">Public Funnel :10000</option>
              <option value="private">Tailnet only :10000</option>
              <option value="off">Disabled</option>
            </select></label>
          </div>
          <label className="switch-row"><input type="checkbox" checked={clearLegacy}
            onChange={(e) => setClearLegacy(e.target.checked)} />
            <span><strong>Remove legacy HTTPS :443 API route</strong>
              <small>{legacy443 ? 'A legacy route to port 8000 appears to be active.' : 'No known legacy route detected.'}</small></span></label>
          {effectiveMcpMode === 'public' && <div className="exposure-warning">
            <AlertTriangle size={16} /><span><strong>Public internet exposure</strong>
              MCP remains authenticated, but this route is reachable outside your tailnet.</span></div>}
          <div className="network-actions">
            <button className="primary no-margin" disabled={!!busy} onClick={() => void apply()}>
              {busy === 'apply' ? <RefreshCw className="spin" size={14} /> : <Save size={14} />} Apply routes
            </button>
            <button className="secondary no-margin" disabled={!!busy} onClick={() => void applySecureDefaults()}
              title="Private dashboard, tailnet-only MCP, and remove the legacy :443 route.">
              {busy === 'secure' ? <RefreshCw className="spin" size={14} /> : <Shield size={14} />} Secure defaults
            </button>
          </div>
          {message && <div className="setup-message">{message}</div>}
        </div>

        <div className="tool-card">
          <div className="setup-card-title"><div><Router size={19} /><div>
            <span className="eyebrow">CONNECTIONS</span><h3>Copy known-good endpoints</h3></div></div></div>
          <div className="endpoint-list">
            {routes.map(([name, value]) => <div key={name}>
              <div><strong>{name}</strong><code>{value}</code></div>
              <button className="icon-button" disabled={value === 'Unavailable'}
                aria-label={'Copy ' + name}
                title={'Copy ' + name}
                onClick={() => void copy(value)}><Copy size={14} /></button>
            </div>)}
          </div>
          <p>Browser API traffic should normally use the dashboard proxy at <code>/api/v1/…</code>.
            Direct port 8090 access requires the API bearer token.</p>
        </div>
      </div>
      <div className="tool-card api-contract">
        <div className="setup-card-title"><div><Settings2 size={19} /><div>
          <span className="eyebrow">UNIFIED API</span><h3>OpenAI-style local endpoints</h3></div></div>
          <StateBadge state="ready" /></div>
        <div className="api-endpoint-grid">
          <code>POST /api/v1/chat/completions</code>
          <code>POST /api/v1/audio/transcriptions</code>
          <code>POST /api/v1/audio/speech</code>
          <code>POST /api/v1/vision/analyze</code>
          <code>GET /api/v1/models</code>
          <code>GET /api/v1/system/status</code>
        </div>
      </div>

      <div className="tool-card actual-routes">
        <div className="setup-card-title"><div><Wifi size={19} /><div>
          <span className="eyebrow">ACTUAL HOST STATE</span><h3>Active Tailscale routes</h3></div></div></div>
        <div className="route-card-grid">
          {routeCards.map((route) => <div className="route-card" key={route.port}>
            <div className="route-port">{route.port}</div>
            <div><strong>{route.title}</strong><span>{route.target}</span></div>
            <b className={route.state === 'warn' ? 'danger-text' : ''}>{route.exposure}</b>
            <StateBadge state={route.state} />
          </div>)}
        </div>
        <details className="raw-state">
          <summary>Advanced: raw Tailscale Serve / Funnel state</summary>
          <pre>{props.network?.serve_status || 'No route state available.'}</pre>
        </details>
        {props.network?.status_error && <div className="error-banner">{props.network.status_error}</div>}
      </div>
    </section>
  )
}
