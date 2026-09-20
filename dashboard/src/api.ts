export type ServiceMetric = {
  last_start_s?: number
  starts?: number
  last_started_at?: number
  last_error?: string | null
  last_error_at?: number
}

export type SupervisorStatus = {
  gpu_owner: string | null
  running_gpu_services: string[]
  active_jobs: Record<string, number>
  lease_epoch: number
  heartbeat?: number
  idle_stop_in_seconds: Record<string, number>
  services: Record<string, string>
  service_states?: Record<string, string>
  service_metrics?: Record<string, ServiceMetric>
}

export type ModelInfo = {
  id: string
  owned_by: string
  capabilities: string[]
  metadata: Record<string, unknown>
}

export type GpuTelemetry = {
  name: string
  utilization_percent: number
  vram_used_mib: number
  vram_total_mib: number
  temperature_c: number
  power_w: number
  power_limit_w: number
  core_clock_mhz: number
  memory_clock_mhz: number
}

export type SystemTelemetry = {
  cpu_percent: number
  ram_used_mib: number
  ram_total_mib: number
  ram_percent: number
  swap_used_mib: number
  swap_total_mib: number
  disk_used_gib: number
  disk_total_gib: number
  disk_free_gib: number
  network_sent_mib: number
  network_recv_mib: number
}

export type JobInfo = {
  id: string
  service: string
  path: string
  state: string
  phase: string
  started_at: number
  finished_at?: number
  elapsed_s: number
  last_progress_at: number
  progress_age_s: number
  activity_age_s: number
  stalled_suspected: boolean
  progress_units: number
  metrics: Record<string, number | string | null>
  error?: string
}

export type SelfTestResult = {
  service: string
  state: string
  detail: string
  started_at?: number | null
  finished_at?: number | null
  elapsed_s: number
}

export type SelfTests = {
  run: { state: string; services: string[]; started_at?: number | null; finished_at?: number | null }
  results: Record<string, SelfTestResult>
}

export type ApiCapabilities = {
  name: string
  version: string
  transport: string
  authentication: string
  models: string[]
  endpoints: Record<string, string>
}

export type NetworkStatus = {
  installed: boolean
  online: boolean
  dns_name: string
  tailscale_ips: string[]
  serve_status: string
  status_error?: string | null
  dashboard_url?: string | null
  comfyui_url?: string | null
  wangp_url?: string | null
  mcp_url?: string | null
  dashboard_enabled?: boolean
  studio_enabled?: boolean
  studio_routes?: { comfyui?: boolean; wangp?: boolean }
  mcp_mode?: 'public' | 'private' | 'off'
  legacy_443?: boolean
  route_state?: Record<string, unknown>
  host_agent_version?: string
}

export type PlatformComponent = {
  name: string
  label: string
  state: string
  detail: string
  required: boolean
  payload?: Record<string, unknown>
}

export type PlatformHealth = {
  status: string
  timestamp: number
  components: PlatformComponent[]
}

export type OpenCodeStatus = {
  installed: boolean
  executable?: string | null
  version?: string
  version_error?: string
  config_present: boolean
  server_running: boolean
  server_url: string
  server_info?: Record<string, unknown>
  server_error?: string
  log_tail?: string
  error_tail?: string
}

export type MaintenanceOperation = {
  id: string
  action: string
  state: string
  started_at: number
  finished_at?: number | null
  exit_code?: number | null
  pid?: number
  snapshot?: string | null
  note?: string
  log_tail: string
}

export type RollbackSnapshot = {
  file: string
  timestamp: string
  created_at: number
  comfyui_sha?: string | null
  wangp_sha?: string | null
  images: string[]
}

export type MaintenanceState = {
  operations: MaintenanceOperation[]
  snapshots: RollbackSnapshot[]
  opencode: OpenCodeStatus
}

export type InstallJob = {
  id: string
  repo: string
  filename: string
  target: string
  started_at: number
  state: string
  exit_code?: number | null
  log_tail: string
  elapsed_s?: number
  progress_percent?: number | null
  progress_text?: string
}

export type ModelFile = {
  name: string
  config_path: string
  size_gib?: number | null
}

export type ModelManagement = {
  config: Record<string, Record<string, string>>
  inventory?: Record<string, ModelFile[]>
  installs: InstallJob[]
  installer_available?: boolean
  hf_cli?: string | null
}

export type Snapshot = {
  timestamp: number
  gateway: { status: string; version: string }
  supervisor: SupervisorStatus
  machine: {
    timestamp: number
    gpu: GpuTelemetry | null
    system: SystemTelemetry | null
    gpu_error?: string | null
    system_error?: string | null
    error?: string
  }
  jobs: { active: JobInfo[]; recent: JobInfo[] }
  mqtt: { connected: boolean; last_error?: string | null }
  self_tests?: SelfTests
}

export type DoctorCheck = {
  id: string
  label: string
  status: 'pass' | 'warn' | 'fail'
  detail: string
}

export type RuntimeSettings = {
  mqtt: {
    enabled: boolean
    host: string
    port: number
    username: string
    password?: string
    password_configured?: boolean
    home_assistant_discovery: boolean
    discovery_prefix: string
    publish_interval: number
  }
}

type ModelsResponse = {
  object: 'list'
  data: ModelInfo[]
}

async function parseError(response: Response) {
  const raw = await response.text()
  try {
    const parsed = JSON.parse(raw)
    return parsed.detail || raw
  } catch {
    return raw || response.statusText
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch('/api' + path, {
    ...options,
    headers: {
      ...(options?.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
      ...options?.headers,
    },
  })
  if (!response.ok) throw new Error(await parseError(response))
  return response.json() as Promise<T>
}

export const localAI = {
  health: () => request<{ status: string; version: string }>('/health'),
  status: () => request<SupervisorStatus>('/control/status'),
  snapshot: () => request<Snapshot>('/control/snapshot'),
  models: () => request<ModelsResponse>('/v1/models'),
  capabilities: () => request<ApiCapabilities>('/v1/capabilities'),
  doctor: () => request<{ status: string; checks: DoctorCheck[] }>('/control/doctor'),
  platformHealth: () => request<PlatformHealth>('/control/platform-health'),
  openCode: () => request<OpenCodeStatus>('/control/opencode'),
  startOpenCode: () => request<{ ok: boolean; status: OpenCodeStatus }>(
    '/control/opencode/start', { method: 'POST' },
  ),
  stopOpenCode: () => request<{ ok: boolean; status: OpenCodeStatus }>(
    '/control/opencode/stop', { method: 'POST' },
  ),
  maintenance: () => request<MaintenanceState>('/control/maintenance'),
  startMaintenance: (payload: {
    action: 'update' | 'rollback' | 'burn-in' | 'opencode-smoke'
    snapshot?: string
  }) => request<MaintenanceOperation>('/control/maintenance/start', {
    method: 'POST',
    body: JSON.stringify(payload),
  }),
  maintenanceOperation: (id: string) =>
    request<MaintenanceOperation>('/control/maintenance/' + encodeURIComponent(id)),
  settings: () => request<RuntimeSettings>('/control/settings'),
  updateSettings: (settings: RuntimeSettings) =>
    request<RuntimeSettings>('/control/settings', {
      method: 'PUT',
      body: JSON.stringify(settings),
    }),
  network: () => request<NetworkStatus>('/control/network'),
  configureTailscale: (settings: {
    dashboard_enabled: boolean
    studio_enabled?: boolean
    mcp_mode: 'public' | 'private' | 'off'
    clear_legacy_443?: boolean
  }) => request<{ ok: boolean; status: NetworkStatus }>('/control/network/tailscale', {
    method: 'POST',
    body: JSON.stringify(settings),
  }),
  selfTests: () => request<SelfTests>('/control/self-tests'),
  runSelfTest: (service: string) =>
    request('/control/self-test/' + encodeURIComponent(service), { method: 'POST' }),
  runAllSelfTests: () => request('/control/self-test-all', { method: 'POST' }),
  mqttTest: () => request<{ status: string; topic: string; message_id: number }>(
    '/control/mqtt/test',
    { method: 'POST' },
  ),
  modelManagement: () => request<ModelManagement>('/control/model-management'),
  updateModelConfig: (payload: {
    service: string
    values: Record<string, string>
    recreate?: boolean
  }) => request<{ ok: boolean; config: ModelManagement['config'] }>(
    '/control/model-management/config',
    { method: 'POST', body: JSON.stringify(payload) },
  ),
  installModel: (payload: { repo: string; filename?: string; target: string }) =>
    request<InstallJob>('/control/model-management/install', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  installStatus: (id: string) =>
    request<InstallJob>('/control/model-management/install/' + encodeURIComponent(id)),
  telemetryHistory: (seconds = 120, maxPoints = 900) =>
    request<{
      samples: Snapshot['machine'][]
      retention_seconds?: number
      persistent?: boolean
      history_error?: string | null
    }>(
      '/control/telemetry/history?seconds=' + encodeURIComponent(seconds)
        + '&max_points=' + encodeURIComponent(maxPoints),
    ),
  benchmark: (service: string) =>
    request<Record<string, number | string | null>>(
      '/control/benchmark/' + encodeURIComponent(service),
      { method: 'POST' },
    ),
  start: (service: string) =>
    request('/control/start/' + encodeURIComponent(service), { method: 'POST' }),
  stop: (service: string) =>
    request('/control/stop/' + encodeURIComponent(service), { method: 'POST' }),
  stopAll: () => request('/control/stop-all', { method: 'POST' }),
  logs: (service: string, tail = 200) =>
    request<{ service: string; logs: string }>(
      '/control/logs/' + encodeURIComponent(service) + '?tail=' + encodeURIComponent(tail),
    ),

  async chat(model: string, messages: Array<{ role: string; content: string }>) {
    return request<{
      choices: Array<{ message: { role: string; content: string } }>
    }>('/v1/chat/completions', {
      method: 'POST',
      body: JSON.stringify({ model, messages, stream: false }),
    })
  },

  async speak(input: string) {
    const response = await fetch('/api/v1/audio/speech', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: 'local-tts',
        input,
        response_format: 'wav',
      }),
    })
    if (!response.ok) throw new Error(await parseError(response))
    return response.blob()
  },

  async transcribe(file: File, language?: string) {
    const body = new FormData()
    body.append('file', file)
    body.append('model', 'local-stt')
    body.append('response_format', 'json')
    if (language) body.append('language', language)
    const response = await fetch('/api/v1/audio/transcriptions', {
      method: 'POST',
      body,
    })
    if (!response.ok) throw new Error(await parseError(response))
    return response.json() as Promise<{
      text: string
      language: string
      language_probability: number
      segments: Array<{ start: number; end: number; text: string }>
    }>
  },

  async analyzeVision(file: File, prompt: string) {
    const body = new FormData()
    body.append('image', file)
    body.append('prompt', prompt)
    body.append('max_new_tokens', '700')
    const response = await fetch('/api/v1/vision/analyze', {
      method: 'POST',
      body,
    })
    if (!response.ok) throw new Error(await parseError(response))
    return response.json() as Promise<{ model: string; text: string }>
  },
}
