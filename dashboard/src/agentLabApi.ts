export type AgentLabHealth = {
  status: string
  service: string
  version: string
  active_runs: string[]
  sandbox: string
  evaluator?: {
    status?: string
    service?: string
    version?: string
    active_evaluations?: string[]
    runner?: {
      ok?: boolean
      age_s?: number
      network_isolated?: boolean
    }
    error?: string
  }
}

export type HarnessInfo = {
  id: string
  version: string
  task_types: string[]
  description: string
  destructive: boolean
}
export type AgentBudget = {
  max_iterations: number
  wall_time_minutes: number
  model_tokens: number
}

export type AgentTaskSpec = {
  repository: string
  base_ref: string
  task_type: string
  objective: string
  allowed_harnesses: string[]
  required_harnesses: string[]
  require_failing_baseline: boolean
  allow_test_edits: boolean
  budget: AgentBudget
}

export type AgentRun = {
  id: string
  status: string
  task: AgentTaskSpec
  created_at: string
  updated_at: string
  workspace?: string | null
  base_commit?: string | null
  selected_harness?: string | null
  result?: Record<string, unknown> | null
  error?: string | null
}
export type AgentEvent = {
  id: number
  run_id: string
  ts: string
  kind: string
  payload: Record<string, unknown>
}

export type PromotionCheck = {
  id: string
  status: string
  message: string
}

export type PromotionReview = {
  run_id: string
  eligible_for_manual_promotion: boolean
  candidate_commit?: string | null
  base_commit?: string | null
  current_source_commit?: string | null
  files_changed: string[]
  insertions: number
  deletions: number
  checks: PromotionCheck[]
}

export type AgentEvaluation = Record<string, unknown> & {
  id?: string
  status?: string
  run_id?: string
}
async function parseAgentError(response: Response) {
  const raw = await response.text()
  try {
    const parsed = JSON.parse(raw)
    return parsed.detail || raw
  } catch {
    return raw || response.statusText
  }
}

async function agentRequest<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch('/agent-lab-api' + path, {
    ...options,
    headers: {
      ...(options?.body ? { 'Content-Type': 'application/json' } : {}),
      ...options?.headers,
    },
  })
  if (!response.ok) throw new Error(await parseAgentError(response))
  return response.json() as Promise<T>
}

export const agentLab = {
  health: () => agentRequest<AgentLabHealth>('/health'),
  harnesses: () => agentRequest<HarnessInfo[]>('/harnesses'),
  runs: (limit = 100) =>
    agentRequest<AgentRun[]>('/runs?limit=' + encodeURIComponent(limit)),
  run: (id: string) =>
    agentRequest<AgentRun>('/runs/' + encodeURIComponent(id)),
  events: (id: string) =>
    agentRequest<AgentEvent[]>('/runs/' + encodeURIComponent(id) + '/events'),
  create: (task: AgentTaskSpec, autoStart = false) =>
    agentRequest<AgentRun>('/runs', {
      method: 'POST',
      body: JSON.stringify({ task, auto_start: autoStart }),
    }),
  execute: (id: string) =>
    agentRequest<AgentRun>('/runs/' + encodeURIComponent(id) + '/execute', {
      method: 'POST',
    }),
  cancel: (id: string) =>
    agentRequest<AgentRun>('/runs/' + encodeURIComponent(id) + '/cancel', {
      method: 'POST',
    }),
  cleanup: (id: string) =>
    agentRequest<AgentRun>('/runs/' + encodeURIComponent(id) + '/cleanup', {
      method: 'POST',
    }),
  promotionReview: (id: string) =>
    agentRequest<PromotionReview>(
      '/runs/' + encodeURIComponent(id) + '/promotion-review',
    ),
  evaluate: (id: string) =>
    agentRequest<AgentEvaluation>(
      '/runs/' + encodeURIComponent(id) + '/evaluate',
      { method: 'POST' },
    ),
  evaluations: (id: string) =>
    agentRequest<AgentEvaluation[]>(
      '/runs/' + encodeURIComponent(id) + '/evaluations',
    ),
  cancelEvaluation: (id: string) =>
    agentRequest<AgentEvaluation>(
      '/evaluations/' + encodeURIComponent(id) + '/cancel',
      { method: 'POST' },
    ),
  latestBenchmark: () =>
    agentRequest<Record<string, unknown>>('/benchmarks/latest'),
  benchmarkHistory: (limit = 20) =>
    agentRequest<Array<Record<string, unknown>>>(
      '/benchmarks/history?limit=' + encodeURIComponent(limit),
    ),
}
