import { useCallback, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { LifeBuoy, Monitor, RefreshCw, Shield, Wifi } from 'lucide-react'
import { localAI } from './api'
import type { RemoteAccessStatus, Snapshot } from './api'

type Readiness = 'ready' | 'not-ready' | 'unknown'

function ageSeconds(timestamp: string | number | undefined, now = Date.now()) {
  if (timestamp === undefined) return Number.POSITIVE_INFINITY
  const parsed = typeof timestamp === 'number' ? timestamp * 1000 : Date.parse(timestamp)
  return Number.isFinite(parsed) ? Math.max(0, (now - parsed) / 1000) : Number.POSITIVE_INFINITY
}

function badgeTone(state: string) {
  if (state === 'ready' || state === 'online' || state === 'connected') return 'ok'
  if (state === 'running') return 'live'
  if (state === 'not-ready' || state === 'unavailable' || state === 'broad' || state === 'missing') return 'bad'
  return 'muted'
}

function StatusBadge({ state, label }: { state: string; label?: string }) {
  return <span className={'state-badge ' + badgeTone(state)}><span className="state-light" />{label || state.replaceAll('-', ' ').toUpperCase()}</span>
}

function bootReadiness(status: RemoteAccessStatus): Readiness {
  if (status.tailnet.state === 'disconnected' || status.tailnet.unattended === 'disabled'
    || status.ssh.service === 'stopped' || status.ssh.service === 'missing' || status.ssh.firewall === 'broad') return 'not-ready'
  if (status.tailnet.state === 'connected' && status.tailnet.unattended === 'enabled'
    && status.ssh.service === 'running' && status.ssh.firewall === 'tailnet-only') return 'ready'
  return 'unknown'
}

function desktopReadiness(status: RemoteAccessStatus): Readiness {
  if (status.desktop.session === 'signed-out' || status.desktop.session === 'locked'
    || status.desktop.task === 'absent' || status.desktop.rdc === 'unavailable') return 'not-ready'
  if (status.desktop.session === 'interactive' && ['ready', 'running'].includes(status.desktop.task)
    && status.desktop.rdc === 'ready') return 'ready'
  return 'unknown'
}

function applicationReadiness(snapshot: Snapshot | null, now: number): string {
  if (!snapshot || ageSeconds(snapshot.timestamp, now) > 60) return 'unknown'
  return snapshot.gateway.status === 'online' ? 'online' : 'unavailable'
}

function staleStatus(status: RemoteAccessStatus | null, now: number) {
  return !!status && ageSeconds(status.observed_at_utc, now) > 60
}

function RemoteAccessCard(props: { id: string; label: string; state: string; detail: string; icon: ReactNode }) {
  return (
    <div className="health-card" data-testid={props.id}>
      {props.icon}<div><span>{props.label}</span><small>{props.detail}</small></div>
      <StatusBadge state={props.state} />
    </div>
  )
}

export function RemoteAccessPanel({ application }: { application: Snapshot | null }) {
  const [status, setStatus] = useState<RemoteAccessStatus | null>(null)
  const [unavailable, setUnavailable] = useState(false)
  const [loading, setLoading] = useState(false)
  const [checkedAt, setCheckedAt] = useState<string | null>(null)
  const [now, setNow] = useState(0)
  const stale = now === 0 || staleStatus(status, now)

  const refresh = useCallback(async () => {
    setLoading(true)
    setUnavailable(false)
    try {
      const next = await localAI.remoteAccess()
      setStatus(next)
      setCheckedAt(new Date().toISOString())
    } catch {
      setStatus(null)
      setUnavailable(true)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    let alive = true
    const load = async () => {
      setLoading(true)
      try {
        const next = await localAI.remoteAccess()
        if (alive) {
          setStatus(next)
          setCheckedAt(new Date().toISOString())
          setUnavailable(false)
        }
      } catch {
        if (alive) {
          setStatus(null)
          setUnavailable(true)
        }
      } finally {
        if (alive) setLoading(false)
      }
    }
    void load()
    return () => { alive = false }
  }, [])

  useEffect(() => {
    const initial = window.setTimeout(() => setNow(Date.now()), 0)
    const timer = window.setInterval(() => setNow(Date.now()), 15000)
    return () => { window.clearTimeout(initial); window.clearInterval(timer) }
  }, [])

  const boot = status && !stale ? bootReadiness(status) : 'unknown'
  const desktop = status && !stale ? desktopReadiness(status) : 'unknown'
  const fallback = status && !stale ? status.human_fallback : 'unknown'
  const appState = now === 0 ? 'unknown' : applicationReadiness(application, now)
  const denied = status?.desktop.denied_by_category

  return (
    <section className="tool-card remote-access-panel" aria-label="Remote access readiness">
      <div className="setup-card-title remote-access-title">
        <div><Shield size={19} /><div><span className="eyebrow">RECOVERY PATHS</span><h3>Remote-access readiness</h3></div></div>
        <button className="secondary no-margin" onClick={() => void refresh()} disabled={loading} aria-label="Refresh remote-access status">
          <RefreshCw className={loading ? 'spin' : ''} size={14} /> Refresh status
        </button>
      </div>
      {unavailable && <div className="error-banner" role="status">Remote-access status unavailable</div>}
      {!unavailable && stale && <div className="error-banner" role="status">Remote-access status is stale</div>}
      <div className="network-summary-grid remote-access-grid">
        <RemoteAccessCard
          id="boot-recovery"
          label="BOOT RECOVERY"
          state={boot}
          detail={status && !stale
            ? `Tailnet ${status.tailnet.state} · unattended ${status.tailnet.unattended} · SSH ${status.ssh.service} · firewall ${status.ssh.firewall}`
            : 'SSH and Tailscale recovery evidence'}
          icon={<Wifi size={20} />}
        />
        <RemoteAccessCard
          id="desktop-health"
          label="INTERACTIVE DESKTOP"
          state={desktop}
          detail={status && !stale
            ? `Session ${status.desktop.session} · rdc ${status.desktop.rdc} · task ${status.desktop.task}`
            : 'Signed-in desktop and rdc'}
          icon={<Monitor size={20} />}
        />
        <RemoteAccessCard
          id="human-fallback"
          label="HUMAN FALLBACK"
          state={fallback}
          detail={`RustDesk direct-IP · ${fallback}`}
          icon={<LifeBuoy size={20} />}
        />
        <RemoteAccessCard
          id="application-health"
          label="AI-STACK APPLICATION"
          state={appState}
          detail={appState === 'online' ? 'Gateway snapshot online' : appState === 'unavailable' ? 'Gateway snapshot unavailable' : 'Application snapshot unknown'}
          icon={<Shield size={20} />}
        />
      </div>
      <div className="remote-access-audit">
        <span>LAST ACCESS AUDIT</span>
        <strong>{status && !stale ? `${status.last_audit.category} · ${status.last_audit.timestamp}` : 'unknown'}</strong>
        <small>Denied actions · authorization {denied?.authorization ?? 'unknown'} · view only {denied?.view_only ?? 'unknown'} · other {denied?.other ?? 'unknown'}</small>
      </div>
      {checkedAt && <small className="remote-access-checked">Checked {new Date(checkedAt).toLocaleTimeString()}</small>}
    </section>
  )
}
