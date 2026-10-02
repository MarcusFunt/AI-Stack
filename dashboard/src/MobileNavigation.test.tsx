import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { MobileNavigation } from './MobileNavigation'

describe('mobile navigation', () => {
  it('keeps the four common destinations visible and marks the current page', () => {
    render(<MobileNavigation section="voice" title="Realtime voice" gatewayOnline onNavigate={vi.fn()} />)

    const navigation = screen.getByRole('navigation', { name: 'Primary mobile navigation' })
    expect(within(navigation).getAllByRole('button').map((button) => button.textContent)).toEqual([
      'Home', 'Chat', 'Speech', 'Call', 'More',
    ])
    expect(within(navigation).getByRole('button', { name: 'Call' })).toHaveAttribute('aria-current', 'page')
  })

  it('opens grouped secondary pages and navigates to a selected page', () => {
    const onNavigate = vi.fn()
    render(<>
      <MobileNavigation section="voice" title="Realtime voice" gatewayOnline onNavigate={onNavigate} />
      <main id="main-content" aria-label="Models" tabIndex={-1}>
        <div className="main-content"><h2>Models</h2></div>
      </main>
    </>)

    const moreButton = screen.getByRole('button', { name: 'More' })
    fireEvent.click(moreButton)
    expect(moreButton).toHaveAttribute('aria-expanded', 'true')
    const extraPages = screen.getByRole('navigation', { name: 'Additional navigation' })
    expect(within(extraPages).getByRole('button', { name: 'Getting started' })).toHaveFocus()
    expect(within(extraPages).getByRole('button', { name: 'Robot vision' })).toBeInTheDocument()
    expect(within(extraPages).getByRole('button', { name: 'Models' })).toBeInTheDocument()
    expect(moreButton).not.toHaveAttribute('aria-current')
    expect(within(extraPages).getAllByRole('button').filter((button) => (
      button.getAttribute('aria-label') !== 'Close additional navigation'
    ))).toHaveLength(11)

    fireEvent.click(within(extraPages).getByRole('button', { name: 'Models' }))
    expect(onNavigate).toHaveBeenCalledWith('models')
    expect(screen.queryByRole('navigation', { name: 'Additional navigation' })).not.toBeInTheDocument()
    expect(moreButton).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByRole('heading', { name: 'Models' })).toHaveFocus()
  })

  it('marks secondary pages as current and closes the menu on Escape', () => {
    render(<MobileNavigation section="models" title="Models" gatewayOnline={false} onNavigate={vi.fn()} />)

    const moreButton = screen.getByRole('button', { name: 'More' })
    expect(moreButton).not.toHaveAttribute('aria-current')
    fireEvent.click(moreButton)
    expect(screen.getByRole('button', { name: 'Models' })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('button', { name: 'Getting started' })).toHaveFocus()
    fireEvent.keyDown(window, { key: 'Escape' })

    expect(screen.queryByRole('navigation', { name: 'Additional navigation' })).not.toBeInTheDocument()
    expect(moreButton).toHaveFocus()
    expect(screen.getByText('Offline')).toBeInTheDocument()
  })

  it('removes mobile navigation while a call is active', () => {
    render(<MobileNavigation
      section="voice"
      title="Realtime voice"
      gatewayOnline
      callActive
      onNavigate={vi.fn()}
    />)

    expect(screen.queryByRole('navigation', { name: 'Primary mobile navigation' })).not.toBeInTheDocument()
    expect(screen.queryByRole('banner')).not.toBeInTheDocument()
  })

  it('focuses the main landmark if the destination has no heading yet', () => {
    render(<>
      <MobileNavigation section="voice" title="Realtime voice" gatewayOnline onNavigate={vi.fn()} />
      <main id="main-content" aria-label="Studio" tabIndex={-1}>
        <div className="main-content"><p>Loading studio…</p></div>
      </main>
    </>)

    fireEvent.click(screen.getByRole('button', { name: 'More' }))
    fireEvent.click(screen.getByRole('button', { name: /^Studio$/ }))

    expect(screen.getByRole('main', { name: 'Studio' })).toHaveFocus()
  })

  it('treats guided startup as the Home destination', () => {
    render(<MobileNavigation section="setup" title="Getting started" gatewayOnline onNavigate={vi.fn()} />)

    expect(screen.getByRole('button', { name: 'Home' })).toHaveAttribute('aria-current', 'page')
    expect(within(screen.getByRole('banner')).getByText('Getting started')).toBeInTheDocument()
  })
})
