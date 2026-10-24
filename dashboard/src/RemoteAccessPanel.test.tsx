import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { RemoteAccessPanel } from './RemoteAccessPanel'

const mocks = vi.hoisted(() => ({ remoteAccess: vi.fn() }))

vi.mock('./api', () => ({ localAI: { remoteAccess: mocks.remoteAccess } }))

function remoteStatus(overrides: Record<string, unknown> = {}) {
  return {
    observed_at_utc: new Date().toISOString(),
    tailnet: { state: 'connected', unattended: 'enabled', last_state_change_utc: '2026-10-24T11:00:00Z' },
    ssh: { service: 'running', firewall: 'tailnet-only', last_listener_check_utc: '2026-10-24T11:59:00Z' },
    desktop: {
      task: 'running', session: 'interactive', rdc: 'ready',
      denied_by_category: { authorization: 2, view_only: 1, other: 0 },
    },
    human_fallback: 'unknown',
    host: { uptime_seconds: 3600, last_boot_utc: '2026-10-24T11:00:00Z' },
    last_audit: { timestamp: '2026-10-24T11:59:30Z', category: 'view_only' },
    ...overrides,
  }
}

describe('RemoteAccessPanel', () => {
  beforeEach(() => mocks.remoteAccess.mockReset().mockResolvedValue(remoteStatus()))

  it('shows boot recovery, desktop, fallback, and app state as separate signals', async () => {
    mocks.remoteAccess.mockResolvedValue(remoteStatus({
      ssh: { service: 'missing', firewall: 'unknown', last_listener_check_utc: 'unknown' },
    }))
    render(<RemoteAccessPanel application={{ timestamp: Date.now() / 1000, gateway: { status: 'online' } } as never} />)

    await waitFor(() => expect(within(screen.getByTestId('boot-recovery')).getByText('NOT READY')).toBeTruthy())
    expect(within(screen.getByTestId('application-health')).getByText('ONLINE')).toBeTruthy()
    expect(within(screen.getByTestId('desktop-health')).getByText('READY')).toBeTruthy()
    expect(within(screen.getByTestId('human-fallback')).getByText('UNKNOWN')).toBeTruthy()
  })

  it('keeps missing recovery evidence unknown even when the app snapshot is healthy', async () => {
    mocks.remoteAccess.mockResolvedValue(remoteStatus({
      tailnet: { state: 'connected', unattended: 'unknown', last_state_change_utc: 'unknown' },
      ssh: { service: 'unknown', firewall: 'unknown', last_listener_check_utc: 'unknown' },
      desktop: { task: 'unknown', session: 'unknown', rdc: 'unknown', denied_by_category: { authorization: null, view_only: null, other: null } },
    }))
    render(<RemoteAccessPanel application={{ timestamp: Date.now() / 1000, gateway: { status: 'online' } } as never} />)

    await waitFor(() => expect(within(screen.getByTestId('boot-recovery')).getByText('UNKNOWN')).toBeTruthy())
    await waitFor(() => expect(within(screen.getByTestId('application-health')).getByText('ONLINE')).toBeTruthy())
    expect(within(screen.getByTestId('desktop-health')).getByText('UNKNOWN')).toBeTruthy()
  })

  it('shows unavailable after the status request fails and can refresh manually', async () => {
    mocks.remoteAccess.mockRejectedValueOnce(new Error('host agent unavailable')).mockResolvedValueOnce(remoteStatus())
    render(<RemoteAccessPanel application={null} />)

    await screen.findByText('Remote-access status unavailable')
    fireEvent.click(screen.getByRole('button', { name: 'Refresh remote-access status' }))
    await waitFor(() => expect(within(screen.getByTestId('boot-recovery')).getByText('READY')).toBeTruthy())
    expect(mocks.remoteAccess).toHaveBeenCalledTimes(2)
  })

  it('does not show stale status as ready', async () => {
    const stale = remoteStatus({ observed_at_utc: new Date(Date.now() - 5 * 60_000).toISOString() })
    mocks.remoteAccess.mockResolvedValue(stale)
    render(<RemoteAccessPanel application={null} />)

    await screen.findByText('Remote-access status is stale')
    expect(within(screen.getByTestId('boot-recovery')).getByText('UNKNOWN')).toBeTruthy()
    expect(screen.queryByText('READY')).toBeNull()
  })
})
