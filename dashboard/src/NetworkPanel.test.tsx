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
    route_state_available: true,
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

  it('enables voice TURN only when the loopback STUN listener is ready', async () => {
    const onNetwork = vi.fn()
    render(<NetworkPanel network={network({ voice_turn_listener_ready: true })} onNetwork={onNetwork} />)

    const toggle = screen.getByRole('checkbox', { name: /Tailnet voice relay on :8447/i })
    expect((toggle as HTMLInputElement).checked).toBe(false)
    expect((toggle as HTMLInputElement).disabled).toBe(false)
    expect(screen.getByText(/UDP relay stays inside the isolated Docker voice network/i)).toBeTruthy()
    fireEvent.click(toggle)
    fireEvent.click(screen.getByRole('button', { name: 'Apply routes' }))

    await waitFor(() => expect(mocks.configureTailscale).toHaveBeenCalledWith({
      dashboard_enabled: true,
      studio_enabled: false,
      mcp_mode: 'off',
      clear_legacy_443: false,
      voice_turn_enabled: true,
    }))
  })

  it('keeps a stale route visible and unavailable when the STUN listener is missing', () => {
    const onNetwork = vi.fn()
    render(<NetworkPanel network={network({
      voice_turn_enabled: false,
      voice_turn_route_present: true,
      voice_turn_listener_ready: false,
    })} onNetwork={onNetwork} />)

    const toggle = screen.getByRole('checkbox', { name: /Tailnet voice relay on :8447/i })
    expect((toggle as HTMLInputElement).checked).toBe(false)
    expect((toggle as HTMLInputElement).disabled).toBe(true)
    const routeCard = within(screen.getByText('Voice TURN relay').closest('.route-card') as HTMLElement)
    expect(routeCard.getByText('INVALID · REMOVAL PENDING')).toBeTruthy()
  })

  it('shows a Funnel route as public and pending removal', () => {
    const onNetwork = vi.fn()
    render(<NetworkPanel network={network({
      voice_turn_enabled: false,
      voice_turn_route_present: true,
      voice_turn_funnel_enabled: true,
      voice_turn_listener_ready: false,
    })} onNetwork={onNetwork} />)

    const routeCard = within(screen.getByText('Voice TURN relay').closest('.route-card') as HTMLElement)
    expect(routeCard.getByText('PUBLIC FUNNEL · REMOVAL PENDING')).toBeTruthy()
  })

  it('shows the route state as unknown and disables TURN control when Serve status is unavailable', () => {
    render(<NetworkPanel network={network({
      route_state_available: false,
      voice_turn_listener_ready: true,
    })} onNetwork={vi.fn()} />)

    const routeCard = within(screen.getByText('Voice TURN relay').closest('.route-card') as HTMLElement)
    expect(routeCard.getByText('UNKNOWN · STATUS UNAVAILABLE')).toBeTruthy()
    const routeCards = Array.from(document.querySelectorAll('.route-card'))
    expect(routeCards.length).toBeGreaterThan(0)
    expect(routeCards.every((card) => card.textContent?.includes('UNKNOWN · STATUS UNAVAILABLE'))).toBe(true)
    expect((screen.getByRole('checkbox', { name: /Tailnet voice relay on :8447/i }) as HTMLInputElement).disabled).toBe(true)
    expect((screen.getByRole('checkbox', { name: /Private dashboard on :8443/i }) as HTMLInputElement).disabled).toBe(true)
    expect((screen.getByLabelText('MCP exposure') as HTMLSelectElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: 'Apply routes' }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: /Secure defaults/i }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('preserves a selected TURN route when applying secure defaults', async () => {
    const onNetwork = vi.fn()
    render(<NetworkPanel network={network({
      voice_turn_enabled: true,
      voice_turn_route_present: true,
      voice_turn_listener_ready: true,
    })} onNetwork={onNetwork} />)
    fireEvent.click(screen.getByRole('button', { name: 'Secure defaults' }))

    await waitFor(() => expect(mocks.configureTailscale).toHaveBeenCalledWith({
      dashboard_enabled: true,
      studio_enabled: true,
      mcp_mode: 'private',
      clear_legacy_443: true,
      voice_turn_enabled: true,
    }))
  })
})
