import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Activity, FlaskConical, GitBranch, Play, RefreshCw, Trash2, XCircle,
} from 'lucide-react'
import { StateBadge } from './OpsPanels'
import {
  agentLab,
  type AgentEvaluation,
  type AgentEvent,
  type AgentLabHealth,
  type AgentRun,
  type HarnessInfo,
  type PromotionReview,
} from './agentLabApi'

const terminalStates = new Set(['passed', 'failed', 'cancelled', 'error'])

function shortSha(value?: string | null) {
  return value ? value.slice(0, 9) : '—'
}

function dateTime(value?: string) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

function statusForBadge(status?: string) {
  if (!status) return 'stopped'
  if (status === 'passed' || status === 'ok' || status === 'ready') return 'ready'
  if (status === 'running') return 'running'
  if (status === 'preparing' || status === 'cancelling') return 'starting'
  if (status === 'failed' || status === 'error') return 'error'
  return status
}
function evaluationId(item: AgentEvaluation) {
  return typeof item.id === 'string' ? item.id : ''
}

function evaluationStatus(item: AgentEvaluation) {
  return typeof item.status === 'string' ? item.status : 'unknown'
}

export function AgentLabPanel() {
  const [health, setHealth] = useState<AgentLabHealth | null>(null)
  const [harnesses, setHarnesses] = useState<HarnessInfo[]>([])
  const [runs, setRuns] = useState<AgentRun[]>([])
  const [selectedId, setSelectedId] = useState('')
  const [selected, setSelected] = useState<AgentRun | null>(null)
  const [events, setEvents] = useState<AgentEvent[]>([])
  const [review, setReview] = useState<PromotionReview | null>(null)
  const [evaluations, setEvaluations] = useState<AgentEvaluation[]>([])
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')

  const [objective, setObjective] = useState('')
  const [taskType, setTaskType] = useState('general')
  const [baseRef, setBaseRef] = useState('HEAD')
  const [harness, setHarness] = useState('')
  const [iterations, setIterations] = useState(8)
  const [wallMinutes, setWallMinutes] = useState(60)
  const [tokenBudget, setTokenBudget] = useState(100000)
  const [requireFailingBaseline, setRequireFailingBaseline] = useState(true)
  const [allowTestEdits, setAllowTestEdits] = useState(false)
  const [autoStart, setAutoStart] = useState(true)
  const loadOverview = useCallback(async () => {
    try {
      const [nextHealth, nextHarnesses, nextRuns] = await Promise.all([
        agentLab.health(),
        agentLab.harnesses(),
        agentLab.runs(100),
      ])
      setHealth(nextHealth)
      setHarnesses(nextHarnesses)
      setRuns(nextRuns)
      setError('')
      if (!selectedId && nextRuns.length) setSelectedId(nextRuns[0].id)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [selectedId])

  const loadSelected = useCallback(async (id: string) => {
    if (!id) {
      setSelected(null)
      setEvents([])
      setReview(null)
      setEvaluations([])
      return
    }
    try {
      const [run, nextEvents] = await Promise.all([
        agentLab.run(id),
        agentLab.events(id),
      ])
      setSelected(run)
      setEvents(nextEvents)
      if (run.status === 'passed') {
        const [nextReview, nextEvaluations] = await Promise.all([
          agentLab.promotionReview(id).catch(() => null),
          agentLab.evaluations(id).catch(() => []),
        ])
        setReview(nextReview)
        setEvaluations(nextEvaluations)
      } else {
        setReview(null)
        setEvaluations([])
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [])

  useEffect(() => {
    const initial = window.setTimeout(() => void loadOverview(), 0)
    const timer = window.setInterval(() => void loadOverview(), 5000)
    return () => {
      window.clearTimeout(initial)
      window.clearInterval(timer)
    }
  }, [loadOverview])

  useEffect(() => {
    if (!selectedId) return
    const initial = window.setTimeout(() => void loadSelected(selectedId), 0)
    const timer = window.setInterval(() => void loadSelected(selectedId), 3500)
    return () => {
      window.clearTimeout(initial)
      window.clearInterval(timer)
    }
  }, [loadSelected, selectedId])

  const orderedRuns = useMemo(
    () => [...runs].sort((a, b) => b.updated_at.localeCompare(a.updated_at)),
    [runs],
  )

  async function createRun() {
    if (!objective.trim()) return
    setBusy('create')
    setError('')
    try {
      const run = await agentLab.create({
        repository: 'ai-stack',
        base_ref: baseRef.trim() || 'HEAD',
        task_type: taskType.trim() || 'general',
        objective: objective.trim(),
        allowed_harnesses: [],
        required_harnesses: harness ? [harness] : [],
        require_failing_baseline: requireFailingBaseline,
        allow_test_edits: allowTestEdits,
        budget: {
          max_iterations: iterations,
          wall_time_minutes: wallMinutes,
          model_tokens: tokenBudget,
        },
      }, autoStart)
      setObjective('')
      setSelectedId(run.id)
      await loadOverview()
      await loadSelected(run.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy('')
    }
  }
  async function runAction(action: 'execute' | 'cancel' | 'cleanup') {
    if (!selected) return
    setBusy(action)
    setError('')
    try {
      const next = await agentLab[action](selected.id)
      setSelected(next)
      await loadOverview()
      await loadSelected(selected.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy('')
    }
  }

  async function evaluateCandidate() {
    if (!selected) return
    setBusy('evaluate')
    setError('')
    try {
      await agentLab.evaluate(selected.id)
      setEvaluations(await agentLab.evaluations(selected.id))
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy('')
    }
  }

  async function cancelEvaluation(id: string) {
    setBusy('eval-' + id)
    try {
      await agentLab.cancelEvaluation(id)
      if (selected) setEvaluations(await agentLab.evaluations(selected.id))
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy('')
    }
  }

  const result = selected?.result || {}
  const evaluatorAvailable = health?.evaluator?.status === 'ok'
  const appliedEdits = Array.isArray(result.applied_edits)
    ? result.applied_edits.map(String)
    : []
  return (
    <section className="workspace agent-lab-page">
      <div className="workspace-head">
        <div>
          <span className="eyebrow">AUTONOMOUS DEVELOPMENT</span>
          <h2>Agent Lab</h2>
          <p className="agent-lab-subtitle">
            Isolated coding runs, harness evidence, candidate review and evaluator status.
          </p>
        </div>
        <button className="secondary no-margin" onClick={() => void loadOverview()}>
          <RefreshCw size={14} /> Refresh
        </button>
      </div>

      <div className="agent-health-grid">
        <div className="health-card">
          <FlaskConical size={19} />
          <div><span>CONTROLLER</span><strong>Agent Lab {health?.version || '—'}</strong>
            <small>{health?.active_runs.length || 0} active run(s)</small></div>
          <StateBadge state={statusForBadge(health?.status)} />
        </div>
        <div className="health-card">
          <Activity size={19} />
          <div><span>SANDBOX</span><strong>{health?.sandbox || 'Unavailable'}</strong>
            <small>Isolated repository test execution</small></div>
          <StateBadge state={statusForBadge(health?.sandbox)} />
        </div>
        <div className="health-card">
          <GitBranch size={19} />
          <div><span>EVALUATOR</span><strong>{health?.evaluator?.status || 'Unavailable'}</strong>
            <small>{health?.evaluator?.runner?.network_isolated ? 'Runner network isolated' : 'Runner state unknown'}</small></div>
          <StateBadge state={statusForBadge(health?.evaluator?.status)} />
        </div>
        <div className="health-card">
          <FlaskConical size={19} />
          <div><span>HARNESSES</span><strong>{harnesses.length} registered</strong>
            <small>{harnesses.map((item) => item.id).join(' · ') || 'None reported'}</small></div>
          <StateBadge state={harnesses.length ? 'ready' : 'stopped'} />
        </div>
      </div>
      <div className="agent-lab-layout">
        <div className="agent-lab-left">
          <div className="tool-card agent-create-card">
            <div className="setup-card-title">
              <div><FlaskConical size={18} /><div>
                <span className="eyebrow">NEW RUN</span><h3>Define an objective</h3>
              </div></div>
            </div>
            <label className="agent-field agent-objective">Objective
              <textarea
                value={objective}
                onChange={(event) => setObjective(event.target.value)}
                placeholder="Describe a concrete change the local coding agent should make and verify…"
              />
            </label>
            <div className="agent-form-grid">
              <label className="agent-field">Task type
                <input value={taskType} onChange={(event) => setTaskType(event.target.value)} />
              </label>
              <label className="agent-field">Base ref
                <input value={baseRef} onChange={(event) => setBaseRef(event.target.value)} />
              </label>
              <label className="agent-field">Required harness
                <select value={harness} onChange={(event) => setHarness(event.target.value)}>
                  <option value="">Auto-select</option>
                  {harnesses.map((item) => <option key={item.id} value={item.id}>{item.id}</option>)}
                </select>
              </label>
              <label className="agent-field">Iterations
                <input type="number" min="1" max="50" value={iterations}
                  onChange={(event) => setIterations(Number(event.target.value))} />
              </label>
              <label className="agent-field">Wall time
                <input type="number" min="1" max="1440" value={wallMinutes}
                  onChange={(event) => setWallMinutes(Number(event.target.value))} />
                <small>minutes</small>
              </label>
              <label className="agent-field">Model tokens
                <input type="number" min="1" step="1000" value={tokenBudget}
                  onChange={(event) => setTokenBudget(Number(event.target.value))} />
              </label>
            </div>
            <div className="agent-toggle-row">
              <label><input type="checkbox" checked={requireFailingBaseline}
                onChange={(event) => setRequireFailingBaseline(event.target.checked)} />
                Require failing baseline</label>
              <label><input type="checkbox" checked={allowTestEdits}
                onChange={(event) => setAllowTestEdits(event.target.checked)} />
                Allow test edits</label>
              <label><input type="checkbox" checked={autoStart}
                onChange={(event) => setAutoStart(event.target.checked)} />
                Start immediately</label>
            </div>
            <button className="primary agent-create-button"
              disabled={busy !== '' || !objective.trim()} onClick={() => void createRun()}>
              {busy === 'create' ? <RefreshCw className="spin" size={14} /> : <Play size={14} />}
              Create {autoStart ? '& start' : 'run'}
            </button>
          </div>

          <div className="tool-card agent-run-list-card">
            <div className="setup-card-title">
              <div><Activity size={18} /><div>
                <span className="eyebrow">RUN HISTORY</span><h3>{orderedRuns.length} recorded runs</h3>
              </div></div>
            </div>
            <div className="agent-run-list">
              {orderedRuns.length === 0 && (
                <div className="agent-empty">No Agent Lab runs have been recorded yet.</div>
              )}
              {orderedRuns.map((run) => (
                <button key={run.id}
                  className={'agent-run-row ' + (selectedId === run.id ? 'selected' : '')}
                  onClick={() => setSelectedId(run.id)}>
                  <div className="agent-run-status"><StateBadge state={statusForBadge(run.status)} /></div>
                  <div className="agent-run-copy">
                    <strong>{run.task.objective}</strong>
                    <span>{run.task.task_type} · {run.selected_harness || 'auto harness'} · {shortSha(run.base_commit)}</span>
                  </div>
                  <time>{dateTime(run.updated_at)}</time>
                </button>
              ))}
            </div>
          </div>
        </div>

        <div className="agent-lab-detail">
          {!selected && (
            <div className="tool-card agent-detail-empty">
              <FlaskConical size={28} />
              <strong>Select a run</strong>
              <span>Run details, evidence and event history will appear here.</span>
            </div>
          )}
          {selected && (
            <>
              <div className="tool-card agent-run-detail">
                <div className="agent-detail-head">
                  <div>
                    <span className="eyebrow">RUN {selected.id.slice(0, 8)}</span>
                    <h3>{selected.task.objective}</h3>
                  </div>
                  <StateBadge state={statusForBadge(selected.status)} />
                </div>
                <div className="agent-run-facts">
                  <div><span>BASE</span><strong>{shortSha(selected.base_commit)}</strong></div>
                  <div><span>HARNESS</span><strong>{selected.selected_harness || 'Auto'}</strong></div>
                  <div><span>TYPE</span><strong>{selected.task.task_type}</strong></div>
                  <div><span>UPDATED</span><strong>{dateTime(selected.updated_at)}</strong></div>
                </div>
                <div className="agent-actions">
                  {selected.status === 'ready' && (
                    <button className="primary no-margin" disabled={busy !== ''}
                      onClick={() => void runAction('execute')}>
                      <Play size={13} /> Execute
                    </button>
                  )}
                  {['ready', 'running'].includes(selected.status) && (
                    <button className="danger-button no-margin" disabled={busy !== ''}
                      onClick={() => void runAction('cancel')}>
                      <XCircle size={13} /> Cancel
                    </button>
                  )}
                  {terminalStates.has(selected.status) && selected.workspace && (
                    <button className="secondary no-margin" disabled={busy !== ''}
                      onClick={() => void runAction('cleanup')}>
                      <Trash2 size={13} /> Clean workspace
                    </button>
                  )}
                  {selected.status === 'passed' && evaluatorAvailable && (
                    <button className="secondary no-margin" disabled={busy !== ''}
                      onClick={() => void evaluateCandidate()}>
                      <FlaskConical size={13} /> Evaluate candidate
                    </button>
                  )}
                </div>
                {selected.error && <div className="error-banner">{selected.error}</div>}
              </div>
              {selected.result && (
                <div className="tool-card agent-evidence-card">
                  <div className="setup-card-title">
                    <div><Activity size={18} /><div>
                      <span className="eyebrow">RESULT</span><h3>Run evidence</h3>
                    </div></div>
                  </div>
                  <div className="agent-result-grid">
                    <div><span>FINAL</span><strong>{String(result.final_status || selected.status)}</strong></div>
                    <div><span>ITERATIONS</span><strong>{String(result.iterations ?? '—')}</strong></div>
                    <div><span>CANDIDATE</span><strong>{shortSha(String(result.candidate_commit || ''))}</strong></div>
                    <div><span>EDITS</span><strong>{appliedEdits.length}</strong></div>
                  </div>
                  {appliedEdits.length > 0 && (
                    <div className="agent-file-list">
                      {appliedEdits.map((file) => <code key={file}>{file}</code>)}
                    </div>
                  )}
                  {typeof result.plan === 'string' && result.plan && (
                    <details className="agent-json-details">
                      <summary>Agent plan</summary><p>{result.plan}</p>
                    </details>
                  )}
                  <details className="agent-json-details">
                    <summary>Raw result</summary>
                    <pre>{JSON.stringify(selected.result, null, 2)}</pre>
                  </details>
                </div>
              )}
              {selected.status === 'passed' && (
                <div className="tool-card agent-promotion-card">
                  <div className="setup-card-title">
                    <div><GitBranch size={18} /><div>
                      <span className="eyebrow">PROMOTION</span><h3>Candidate review</h3>
                    </div></div>
                    <StateBadge state={review?.eligible_for_manual_promotion ? 'ready' : 'stopped'} />
                  </div>
                  {review ? (
                    <>
                      <div className="agent-promotion-summary">
                        <strong>{review.eligible_for_manual_promotion
                          ? 'Eligible for manual promotion review'
                          : 'Additional evidence required'}</strong>
                        <span>{review.files_changed.length} files · +{review.insertions} / -{review.deletions}</span>
                      </div>
                      <div className="agent-review-checks">
                        {review.checks.map((check) => (
                          <div key={check.id}><StateBadge state={statusForBadge(check.status)} />
                            <span><strong>{check.id}</strong>{check.message && <small>{check.message}</small>}</span>
                          </div>
                        ))}
                      </div>
                    </>
                  ) : <div className="agent-empty">Promotion review is unavailable for this run.</div>}
                </div>
              )}
              {selected.status === 'passed' && evaluatorAvailable && (
                <div className="tool-card agent-evaluation-card">
                  <div className="setup-card-title">
                    <div><FlaskConical size={18} /><div>
                      <span className="eyebrow">EVALUATOR</span><h3>Independent candidate evaluations</h3>
                    </div></div>
                  </div>
                  <div className="agent-evaluation-list">
                    {evaluations.length === 0 && <div className="agent-empty">No evaluator runs yet.</div>}
                    {evaluations.map((evaluation, index) => {
                      const id = evaluationId(evaluation)
                      const status = evaluationStatus(evaluation)
                      return <div key={id || index} className="agent-evaluation-row">
                        <StateBadge state={statusForBadge(status)} />
                        <div><strong>{id ? id.slice(0, 10) : 'evaluation'}</strong>
                          <span>{status}</span></div>
                        {id && ['queued', 'running'].includes(status) && (
                          <button className="secondary no-margin"
                            disabled={busy === 'eval-' + id}
                            onClick={() => void cancelEvaluation(id)}>Cancel</button>
                        )}
                      </div>
                    })}
                  </div>
                </div>
              )}

              <div className="tool-card agent-events-card">
                <div className="setup-card-title">
                  <div><Activity size={18} /><div>
                    <span className="eyebrow">EVENT STREAM</span><h3>{events.length} events</h3>
                  </div></div>
                </div>
                <div className="agent-event-list">
                  {events.length === 0 && <div className="agent-empty">No events recorded.</div>}
                  {[...events].reverse().map((event) => (
                    <div className="agent-event-row" key={event.id}>
                      <div className="agent-event-marker" />
                      <div>
                        <div className="agent-event-title">
                          <strong>{event.kind.replaceAll('_', ' ')}</strong>
                          <time>{dateTime(event.ts)}</time>
                        </div>
                        {Object.keys(event.payload).length > 0 && (
                          <details className="agent-event-payload">
                            <summary>Payload</summary>
                            <pre>{JSON.stringify(event.payload, null, 2)}</pre>
                          </details>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </>
          )}
        </div>
      </div>
      {error && <div className="global-error error-banner">{error}</div>}
    </section>
  )
}
