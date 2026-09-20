import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Activity, BrainCircuit, Download, History, Play, RefreshCw, RotateCcw,
  ShieldCheck, Square, TestTube2, Wrench,
} from 'lucide-react'
import { localAI } from './api'
import type { MaintenanceOperation, MaintenanceState } from './api'
import { StateBadge } from './OpsPanels'

function fmtDuration(seconds: number) {
  if (!Number.isFinite(seconds) || seconds < 0) return '—'
  if (seconds < 60) return Math.round(seconds) + 's'
  const minutes = Math.floor(seconds / 60)
  const rest = Math.floor(seconds % 60)
  return minutes + 'm ' + String(rest).padStart(2, '0') + 's'
}

function fmtTime(value?: number | null) {
  if (!value) return '—'
  return new Date(value * 1000).toLocaleString()
}

function actionLabel(action: string) {
  const labels: Record<string, string> = {
    'burn-in': 'Full burn-in',
    update: 'Dependency update',
    rollback: 'Rollback',
    'opencode-smoke': 'OpenCode smoke test',
  }
  return labels[action] || action
}

function operationElapsed(operation: MaintenanceOperation, now: number) {
  const end = operation.finished_at || now
  return Math.max(0, end - operation.started_at)
}

export function MaintenancePanel() {
  const [data, setData] = useState<MaintenanceState | null>(null)
  const [selectedSnapshot, setSelectedSnapshot] = useState('')
  const [selectedOperationId, setSelectedOperationId] = useState('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [now, setNow] = useState(() => Date.now() / 1000)

  const refresh = useCallback(async () => {
    try {
      const next = await localAI.maintenance()
      setData(next)
      setError('')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [])

  useEffect(() => {
    const initial = window.setTimeout(() => void refresh(), 0)
    const timer = window.setInterval(() => void refresh(), 4000)
    const clock = window.setInterval(() => setNow(Date.now() / 1000), 1000)
    return () => {
      window.clearTimeout(initial)
      window.clearInterval(timer)
      window.clearInterval(clock)
    }
  }, [refresh])

  const activeOperation = data?.operations.find((item) => item.state === 'running')
  const effectiveSnapshot = selectedSnapshot || data?.snapshots[0]?.file || ''
  const selectedOperation = useMemo(() => {
    if (!data?.operations.length) return null
    return data.operations.find((item) => item.id === selectedOperationId)
      || data.operations[0]
  }, [data, selectedOperationId])

  async function controlOpenCode(action: 'start' | 'stop') {
    setBusy('opencode-' + action)
    setMessage('')
    setError('')
    try {
      const result = action === 'start'
        ? await localAI.startOpenCode()
        : await localAI.stopOpenCode()
      setMessage(action === 'start'
        ? 'OpenCode server is ready.'
        : 'OpenCode server stopped.')
      setData((old) => old ? { ...old, opencode: result.status } : old)
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy('')
    }
  }

  async function startOperation(action: 'update' | 'rollback' | 'burn-in' | 'opencode-smoke') {
    if (activeOperation) {
      setError('Another maintenance operation is already running.')
      return
    }
    if (action === 'rollback' && !effectiveSnapshot) {
      setError('No rollback snapshot is available.')
      return
    }

    const confirmations: Partial<Record<typeof action, string>> = {
      update: 'Update stack dependencies now? This rebuilds and recreates services, so the dashboard may disconnect briefly.',
      rollback: 'Roll back to ' + effectiveSnapshot + '? This restores saved third-party revisions and rollback images, then recreates services.',
      'burn-in': 'Start the full burn-in suite? It can take a long time, exercises the full stack, and stops GPU workers when it finishes.',
    }
    const prompt = confirmations[action]
    if (prompt && !window.confirm(prompt)) return

    setBusy(action)
    setMessage('')
    setError('')
    try {
      const operation = await localAI.startMaintenance({
        action,
        ...(action === 'rollback' ? { snapshot: effectiveSnapshot } : {}),
      })
      setSelectedOperationId(operation.id)
      setMessage(actionLabel(action) + ' started. You can leave this page; the host agent keeps the job running.')
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy('')
    }
  }
  const openCode = data?.opencode
  const openCodeState = !openCode?.installed
    ? 'error'
    : openCode.server_running ? 'ready' : 'stopped'

  return (
    <section className="workspace maintenance-page">
      <div className="workspace-head">
        <div>
          <span className="eyebrow">OPERATIONS & RECOVERY</span>
          <h2>Maintenance</h2>
          <p className="maintenance-subtitle">
            Safe host-side controls for OpenCode, validation, dependency updates and rollback.
          </p>
        </div>
        <button className="secondary no-margin" onClick={() => void refresh()}>
          <RefreshCw size={14} /> Refresh
        </button>
      </div>

      <div className="tool-card maintenance-opencode">
        <div className="setup-card-title">
          <div><BrainCircuit size={19} /><div>
            <span className="eyebrow">CODING AGENT</span><h3>OpenCode</h3>
          </div></div>
          <StateBadge state={openCodeState} />
        </div>
        <div className="maintenance-opencode-grid">
          <div>
            <span>INSTALLATION</span>
            <strong>{openCode?.installed ? (openCode.version || 'Installed') : 'Not installed'}</strong>
            <small>{openCode?.config_present ? 'opencode.jsonc present' : 'Configuration missing'}</small>
          </div>
          <div>
            <span>SERVER</span>
            <strong>{openCode?.server_running ? 'Running on localhost:4096' : 'Stopped'}</strong>
            <small>The server remains loopback-only; control is proxied through the authenticated dashboard API.</small>
          </div>
          <div className="maintenance-opencode-actions">
            <button className="secondary no-margin"
              disabled={!!busy || !openCode?.installed || !!openCode?.server_running}
              onClick={() => void controlOpenCode('start')}>
              {busy === 'opencode-start' ? <RefreshCw className="spin" size={13} /> : <Play size={13} />}
              Start server
            </button>
            <button className="secondary no-margin"
              disabled={!!busy || !openCode?.server_running}
              onClick={() => void controlOpenCode('stop')}>
              {busy === 'opencode-stop' ? <RefreshCw className="spin" size={13} /> : <Square size={13} />}
              Stop server
            </button>
            <button className="primary no-margin"
              disabled={!!busy || !openCode?.installed || !!activeOperation}
              onClick={() => void startOperation('opencode-smoke')}>
              {busy === 'opencode-smoke' ? <RefreshCw className="spin" size={13} /> : <ShieldCheck size={13} />}
              End-to-end smoke
            </button>
          </div>
        </div>
        {(openCode?.error_tail || openCode?.log_tail) && (
          <details className="maintenance-details">
            <summary>Recent OpenCode server log</summary>
            <pre>{openCode.error_tail || openCode.log_tail}</pre>
          </details>
        )}
      </div>

      <div className="maintenance-action-grid">
        <div className="tool-card maintenance-action-card">
          <div className="maintenance-action-icon"><TestTube2 size={20} /></div>
          <span className="eyebrow">VALIDATE</span>
          <h3>Full burn-in</h3>
          <p>Runs strict doctor checks, forced gateway-IP proxy recovery, lease concurrency, end-to-end inference smoke, and a final doctor pass.</p>
          <button className="secondary no-margin"
            disabled={!!busy || !!activeOperation}
            onClick={() => void startOperation('burn-in')}>
            {busy === 'burn-in' ? <RefreshCw className="spin" size={13} /> : <Play size={13} />}
            Start burn-in
          </button>
        </div>

        <div className="tool-card maintenance-action-card">
          <div className="maintenance-action-icon"><Download size={20} /></div>
          <span className="eyebrow">UPDATE</span>
          <h3>Refresh dependencies</h3>
          <p>Creates rollback image tags, snapshots ComfyUI/Wan2GP revisions, pulls updates, rebuilds managed images, and recreates the stack.</p>
          <button className="secondary no-margin"
            disabled={!!busy || !!activeOperation}
            onClick={() => void startOperation('update')}>
            {busy === 'update' ? <RefreshCw className="spin" size={13} /> : <Download size={13} />}
            Start update
          </button>
        </div>

        <div className="tool-card maintenance-action-card">
          <div className="maintenance-action-icon"><RotateCcw size={20} /></div>
          <span className="eyebrow">RECOVERY</span>
          <h3>Rollback</h3>
          <p>Restores saved third-party revisions and rollback images from a previous update snapshot, then recreates services.</p>
          <label className="maintenance-snapshot-select">
            Snapshot
            <select value={effectiveSnapshot} onChange={(event) => setSelectedSnapshot(event.target.value)}>
              {data?.snapshots.length ? data.snapshots.map((snapshot) => (
                <option key={snapshot.file} value={snapshot.file}>
                  {snapshot.timestamp} · {snapshot.images.length} images
                </option>
              )) : <option value="">No snapshots available</option>}
            </select>
          </label>
          <button className="danger-button no-margin"
            disabled={!!busy || !!activeOperation || !effectiveSnapshot}
            onClick={() => void startOperation('rollback')}>
            {busy === 'rollback' ? <RefreshCw className="spin" size={13} /> : <History size={13} />}
            Roll back
          </button>
        </div>
      </div>
      <div className="tool-card maintenance-operations">
        <div className="setup-card-title">
          <div><Activity size={19} /><div>
            <span className="eyebrow">HOST OPERATIONS</span><h3>Execution history</h3>
          </div></div>
          {activeOperation && <StateBadge state="running" />}
        </div>

        <div className="maintenance-operation-grid">
          <div className="maintenance-operation-list">
            {!data?.operations.length && (
              <div className="agent-empty">No maintenance operations have been recorded yet.</div>
            )}
            {data?.operations.map((operation) => (
              <button key={operation.id}
                className={'maintenance-operation-row ' + (selectedOperation?.id === operation.id ? 'selected' : '')}
                onClick={() => setSelectedOperationId(operation.id)}>
                <StateBadge state={operation.state} />
                <div>
                  <strong>{actionLabel(operation.action)}</strong>
                  <span>{fmtTime(operation.started_at)} · {fmtDuration(operationElapsed(operation, now))}</span>
                </div>
                <code>{operation.id.slice(-6)}</code>
              </button>
            ))}
          </div>

          <div className="maintenance-operation-detail">
            {selectedOperation ? (
              <>
                <div className="maintenance-operation-head">
                  <div>
                    <span>{actionLabel(selectedOperation.action)}</span>
                    <strong>{selectedOperation.id}</strong>
                  </div>
                  <StateBadge state={selectedOperation.state} />
                </div>
                <div className="maintenance-operation-facts">
                  <span>Started <b>{fmtTime(selectedOperation.started_at)}</b></span>
                  <span>Elapsed <b>{fmtDuration(operationElapsed(selectedOperation, now))}</b></span>
                  <span>Exit <b>{selectedOperation.exit_code ?? '—'}</b></span>
                  {selectedOperation.snapshot && <span>Snapshot <b>{selectedOperation.snapshot}</b></span>}
                </div>
                {selectedOperation.note && <div className="setup-message">{selectedOperation.note}</div>}
                <pre className="maintenance-console">{selectedOperation.log_tail || 'Waiting for output…'}</pre>
              </>
            ) : (
              <div className="agent-empty">
                <Wrench size={24} /> Select an operation to inspect its output.
              </div>
            )}
          </div>
        </div>
      </div>

      {message && <div className="setup-message">{message}</div>}
      {error && <div className="error-banner">{error}</div>}
    </section>
  )
}
