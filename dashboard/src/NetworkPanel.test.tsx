import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { NetworkStatus } from './api'
import { NetworkPanel } from './OpsPanels'

const mocks = vi.hoisted(() => ({
  capabilities: vi.fn(),
  configureTailscale: vi.fn(),
}))

vi.mock('./api', () => ({
  localAI: {
    capabilities: mocks.capabilities,
    configureTailscale: mocks.configureTailscale,
  },
}))

function network(overrides: Partial<NetworkStatus> = {}): NetworkStatus {
  return {
    installed: true,
    online: true,
    dns_name: 'host.tailnet.example',
    tailscale_ips: ['100.64.0.1'],
    serve_status: '',
    dashboard_enabled: true,
    studio_enabled: false,
    studio_routes: { comfyui: false, wangp: false },
    mcp_mode: 'off',
    voice_turn_enabled: false,
    ...overrides,
  }
}

describe('NetworkPanel voice TURN route', () => {
  beforeEach(() => {
    mocks.capabilities.mockReset().mockResolvedValue({
      name: 'AI-Stack', version: 'test', transport: 'HTTP', authentication: 'Bearer',
      models: [], endpoints: {},
    })
    mocks.configureTailscale.mockReset().mockResolvedValue({ ok: true, status: network() })
  })

  it('locks voice TURN off and marks an existing route for removal', () => {
    const onNetwork = vi.fn()
    render(<NetworkPanel network={network({
      voice_turn_enabled: false,
      voice_turn_route_present: true,
    })} onNetwork={onNetwork} />)

    const toggle = screen.getByRole('checkbox', { name: /Tailnet voice relay disabled on :8447/i })
    expect((toggle as HTMLInputElement).checked).toBe(false)
    expect((toggle as HTMLInputElement).disabled).toBe(true)
    const routeCard = within(screen.getByText('Voice TURN relay').closest('.route-card') as HTMLElement)
    expect(routeCard.getByText('BLOCKED · REMOVAL PENDING')).toBeTruthy()
    expect(screen.getByText(/UDP relay path is unavailable/i)).toBeTruthy()
  })

  it('forces voice TURN off when applying other network settings', async () => {
    const onNetwork = vi.fn()
    render(<NetworkPanel network={network({
      voice_turn_enabled: false,
      voice_turn_route_present: true,
    })} onNetwork={onNetwork} />)
    fireEvent.click(screen.getByRole('button', { name: 'Apply routes' }))

    await waitFor(() => expect(mocks.configureTailscale).toHaveBeenCalledWith({
      dashboard_enabled: true,
      studio_enabled: false,
      mcp_mode: 'off',
      clear_legacy_443: false,
      voice_turn_enabled: false,
    }))
  })
})
