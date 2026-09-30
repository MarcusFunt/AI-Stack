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
    mocks.configureTailscale.mockReset().mockResolvedValue({ ok: true, status: network({ voice_turn_enabled: true }) })
  })

  it('exposes an off-by-default toggle and displays the verified host-agent route state', async () => {
    const onNetwork = vi.fn()
    const { rerender } = render(<NetworkPanel network={network()} onNetwork={onNetwork} />)

    const toggle = screen.getByRole('checkbox', { name: /Tailnet voice relay on :8447/i })
    expect((toggle as HTMLInputElement).checked).toBe(false)
    expect(within(screen.getByText('Voice TURN relay').closest('.route-card') as HTMLElement).getByText('OFF')).toBeTruthy()

    rerender(<NetworkPanel network={network({ voice_turn_enabled: true })} onNetwork={onNetwork} />)
    expect((screen.getByRole('checkbox', { name: /Tailnet voice relay on :8447/i }) as HTMLInputElement).checked).toBe(true)
    expect(within(screen.getByText('Voice TURN relay').closest('.route-card') as HTMLElement).getByText('TAILNET ONLY')).toBeTruthy()
  })

  it('sends the opt-in relay setting through the existing authenticated host-agent route', async () => {
    const onNetwork = vi.fn()
    render(<NetworkPanel network={network()} onNetwork={onNetwork} />)
    fireEvent.click(screen.getByRole('checkbox', { name: /Tailnet voice relay on :8447/i }))
    fireEvent.click(screen.getByRole('button', { name: 'Apply routes' }))

    await waitFor(() => expect(mocks.configureTailscale).toHaveBeenCalledWith({
      dashboard_enabled: true,
      studio_enabled: false,
      mcp_mode: 'off',
      clear_legacy_443: false,
      voice_turn_enabled: true,
    }))
  })
})
