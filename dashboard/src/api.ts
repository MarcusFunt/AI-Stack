export type SupervisorStatus = {
  gpu_owner: string | null
  running_gpu_services: string[]
  active_jobs: Record<string, number>
  lease_epoch: number
  idle_stop_in_seconds: Record<string, number>
  services: Record<string, string>
}

export type ModelInfo = {
  id: string
  owned_by: string
  capabilities: string[]
  metadata: Record<string, unknown>
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
  if (!response.ok) {
    throw new Error(await parseError(response))
  }
  return response.json() as Promise<T>
}

export const localAI = {
  health: () => request<{ status: string; version: string }>('/health'),
  status: () => request<SupervisorStatus>('/control/status'),
  models: () => request<ModelsResponse>('/v1/models'),
  start: (service: string) =>
    request('/control/start/' + encodeURIComponent(service), { method: 'POST' }),
  stop: (service: string) =>
    request('/control/stop/' + encodeURIComponent(service), { method: 'POST' }),
  stopAll: () => request('/control/stop-all', { method: 'POST' }),

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
