import { useCallback, useEffect, useState } from 'react'
import { Database, RefreshCw } from 'lucide-react'
import { localAI } from './api'
import type { VisualMemoryStatus } from './api'
import { StateBadge } from './OpsPanels'

function sizeMiB(bytes?: number) {
  if (bytes === undefined || !Number.isFinite(bytes)) return '—'
  return (bytes / (1024 * 1024)).toFixed(1) + ' MiB'
}

function percentile(value: number | null | undefined) {
  return value === null || value === undefined ? '—' : value.toFixed(1) + ' ms'
}

export function VisualMemoryStatusPanel() {
  const [status, setStatus] = useState<VisualMemoryStatus | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      setStatus(await localAI.visualMemoryStatus())
      setError('')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    const initialLoad = window.setTimeout(() => { void refresh() }, 0)
    const timer = window.setInterval(() => { void refresh() }, 60_000)
    return () => {
      window.clearTimeout(initialLoad)
      window.clearInterval(timer)
    }
  }, [refresh])

  const database = status?.database
  const index = status?.index
  const latency = status?.latency_percentiles_ms
  const state = !status ? (error ? 'unavailable' : 'loading') : status.status === 'ok' ? 'ready' : status.status

  return (
    <section className="tool-card visual-memory-status" aria-label="Visual memory status">
      <div className="workspace-head">
        <div><span className="eyebrow">VISUAL RETRIEVAL</span><h3>Visual memory</h3></div>
        <button className="refresh-button" onClick={() => void refresh()} disabled={loading}>
          <RefreshCw size={14} className={loading ? 'spin' : ''} /> Refresh
        </button>
      </div>
      {error && <p className="danger-text" role="alert">{error}</p>}
      <div className="visual-memory-status-grid">
        <div><span>Service</span><StateBadge state={state} /></div>
        <div><span>Embedding model</span><strong>{status?.model || '—'}</strong></div>
        <div><span>Revision</span><code>{status?.revision || '—'}</code></div>
        <div><span>Runtime</span><strong>{status ? `${status.device} · ${status.loaded ? 'loaded' : 'not loaded'}` : '—'}</strong></div>
        <div><span>Model files</span><strong>{status ? (status.model_files_available ? 'available' : 'missing') : '—'}</strong></div>
        <div><span>SQLite records / vectors</span><strong>{database ? `${database.records} / ${database.vectors}` : '—'}</strong></div>
        <div><span>Database</span><strong>{database ? `${database.available ? 'ready' : 'missing'} · ${database.writable ? 'writable' : 'read-only'}` : '—'}</strong></div>
        <div><span>Vector index</span><strong>{index ? `${index.compatible ? 'compatible' : 'needs rebuild'} · ${index.backend}` : '—'}</strong></div>
        <div><span>Database / index size</span><strong>{database && index ? `${sizeMiB(database.size_bytes)} / ${sizeMiB(index.size_bytes)}` : '—'}</strong></div>
        <div><span>Embedding p50 / p95</span><strong>{latency ? `${percentile(latency.embedding.p50)} / ${percentile(latency.embedding.p95)}` : '—'}</strong></div>
        <div><span>Search p50 / p95</span><strong>{latency ? `${percentile(latency.search.p50)} / ${percentile(latency.search.p95)}` : '—'}</strong></div>
      </div>
      <small><Database size={13} /> SQLite remains authoritative; vectors are not displayed.</small>
    </section>
  )
}
