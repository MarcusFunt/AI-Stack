import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MaintenancePanel } from './MaintenancePanel'
import type { MaintenanceOperation, MaintenanceState } from './api'

const mocks = vi.hoisted(() => ({
  maintenance: vi.fn(),
  startMaintenance: vi.fn(),
  startOpenCode: vi.fn(),
  stopOpenCode: vi.fn(),
}))

vi.mock('./api', () => ({
  localAI: {
    maintenance: mocks.maintenance,
    startMaintenance: mocks.startMaintenance,
    startOpenCode: mocks.startOpenCode,
    stopOpenCode: mocks.stopOpenCode,
  },
}))

function state(operations: MaintenanceOperation[] = []): MaintenanceState {
  return {
    operations,
    snapshots: [],
    opencode: {
      installed: true,
      config_present: true,
      server_running: false,
      server_url: 'http://127.0.0.1:4096',
    },
  }
}

const runningOperation: MaintenanceOperation = {
  id: 'op-0123456789ab',
  action: 'voice-smoke',
  state: 'running',
  started_at: 1_700_000_000,
  log_tail: '[voice-smoke] TTS started',
}

describe('MaintenancePanel voice pipeline smoke action', () => {
  beforeEach(() => {
    mocks.maintenance.mockReset()
    mocks.startMaintenance.mockReset()
    mocks.startOpenCode.mockReset()
    mocks.stopOpenCode.mockReset()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
  })

  it('starts on demand and shows the latest operation stage after refresh', async () => {
    mocks.maintenance.mockResolvedValueOnce(state()).mockResolvedValue(state([runningOperation]))
    mocks.startMaintenance.mockResolvedValue(runningOperation)
    render(<MaintenancePanel />)

    fireEvent.click(await screen.findByRole('button', { name: /Run voice smoke/i }))

    await waitFor(() => expect(mocks.startMaintenance).toHaveBeenCalledWith({ action: 'voice-smoke' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Voice pipeline smoke test in progress')
    expect(screen.getByRole('status')).toHaveTextContent('TTS started')
  })

  it('shows an API refusal as an actionable error', async () => {
    mocks.maintenance.mockResolvedValue(state())
    mocks.startMaintenance.mockRejectedValue(new Error('active AI jobs prevent maintenance'))
    render(<MaintenancePanel />)

    fireEvent.click(await screen.findByRole('button', { name: /Run voice smoke/i }))

    expect(await screen.findByText('active AI jobs prevent maintenance')).toBeInTheDocument()
  })

  it('restores host operation progress after the panel reconnects', async () => {
    mocks.maintenance.mockResolvedValue(state([runningOperation]))
    const first = render(<MaintenancePanel />)
    expect(await screen.findByRole('status')).toHaveTextContent('TTS started')
    first.unmount()

    render(<MaintenancePanel />)
    expect(await screen.findByRole('status')).toHaveTextContent('Voice pipeline smoke test in progress')
    expect(screen.getByRole('status')).toHaveTextContent('TTS started')
    expect(mocks.maintenance).toHaveBeenCalledTimes(2)
  })
})
