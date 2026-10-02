import { useEffect, useRef, useState } from 'react'
import { BrainCircuit, MoreHorizontal } from 'lucide-react'
import { mobileMoreGroups, mobilePrimaryNav, type NavigationSection } from './navigation'

type MobileNavigationProps = {
  section: NavigationSection
  title: string
  gatewayOnline: boolean
  callActive?: boolean
  onNavigate: (section: NavigationSection) => void
}

export function MobileNavigation({ section, title, gatewayOnline, callActive = false, onNavigate }: MobileNavigationProps) {
  const [moreOpen, setMoreOpen] = useState(false)
  const moreButtonRef = useRef<HTMLButtonElement>(null)
  const firstMoreItemRef = useRef<HTMLButtonElement>(null)
  const wasMoreOpen = useRef(false)
  const focusAfterDismiss = useRef<'trigger' | 'page'>('trigger')
  const moreSections = mobileMoreGroups.flatMap((group) => group.items.map(([key]) => key))
  const selectedMorePage = moreSections.includes(section)

  useEffect(() => {
    if (!moreOpen) return
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        focusAfterDismiss.current = 'trigger'
        setMoreOpen(false)
      }
    }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [moreOpen])

  useEffect(() => {
    if (moreOpen) {
      wasMoreOpen.current = true
      firstMoreItemRef.current?.focus()
      return
    }
    if (!wasMoreOpen.current) return
    wasMoreOpen.current = false
    if (focusAfterDismiss.current === 'page') {
      const main = document.getElementById('main-content')
      const heading = main?.querySelector<HTMLElement>('h1, h2, h3')
      if (heading) {
        heading.tabIndex = -1
        heading.focus()
      } else {
        main?.focus()
      }
    } else {
      moreButtonRef.current?.focus()
    }
  }, [moreOpen])

  function navigateTo(next: NavigationSection) {
    if (moreOpen) focusAfterDismiss.current = 'page'
    onNavigate(next)
    setMoreOpen(false)
  }

  function closeMore() {
    focusAfterDismiss.current = 'trigger'
    setMoreOpen(false)
  }

  function toggleMore() {
    if (moreOpen) closeMore()
    else {
      focusAfterDismiss.current = 'trigger'
      setMoreOpen(true)
    }
  }

  if (callActive) return null

  return (
    <div className="mobile-shell-navigation">
      <header className="mobile-appbar">
        <div className="mobile-brand-mark" aria-hidden="true"><BrainCircuit size={19} strokeWidth={1.8} /></div>
        <div className="mobile-page-title">
          <span>LOCAL AI</span>
          <strong>{title}</strong>
        </div>
        <div className={'mobile-connection ' + (gatewayOnline ? 'is-online' : 'is-offline')}>
          <span className="mobile-status-dot" aria-hidden="true" />
          <span>{gatewayOnline ? 'Online' : 'Offline'}</span>
        </div>
      </header>

      <nav className="mobile-bottom-nav" aria-label="Primary mobile navigation">
        {mobilePrimaryNav.map(({ section: key, Icon, label }) => {
          const selected = section === key || (key === 'overview' && section === 'setup')
          return (
            <button
              type="button"
              key={key}
              className={selected ? 'active' : ''}
              aria-current={selected ? 'page' : undefined}
              onClick={() => navigateTo(key)}
            >
              <Icon size={19} strokeWidth={1.9} />
              <span>{label}</span>
            </button>
          )
        })}
        <button
          type="button"
          className={moreOpen || selectedMorePage ? 'active' : ''}
          ref={moreButtonRef}
          aria-expanded={moreOpen}
          aria-controls="mobile-more-menu"
          onClick={toggleMore}
        >
          <MoreHorizontal size={20} strokeWidth={2} />
          <span>More</span>
        </button>
      </nav>

      {moreOpen && <button
        className="mobile-menu-dismiss"
        type="button"
        tabIndex={-1}
        aria-hidden="true"
        onClick={closeMore}
      />}
      <nav className="mobile-more-menu" id="mobile-more-menu" aria-label="Additional navigation" hidden={!moreOpen}>
        <div className="mobile-more-heading">
          <div><span>EXPLORE</span><strong>More sections</strong></div>
          <button type="button" aria-label="Close additional navigation" onClick={closeMore}>×</button>
        </div>
        <div className="mobile-more-groups">
          {mobileMoreGroups.map((group, groupIndex) => (
            <section className="mobile-more-group" key={group.label}>
              <h2>{group.label}</h2>
              <div className="mobile-more-links">
                {group.items.map(([key, Icon, label], itemIndex) => (
                  <button
                    type="button"
                    key={key}
                    className={section === key ? 'active' : ''}
                    aria-current={section === key ? 'page' : undefined}
                    ref={groupIndex === 0 && itemIndex === 0 ? firstMoreItemRef : undefined}
                    onClick={() => navigateTo(key)}
                  >
                    <Icon size={17} strokeWidth={1.8} />
                    <span>{label}</span>
                  </button>
                ))}
              </div>
            </section>
          ))}
        </div>
      </nav>
    </div>
  )
}
