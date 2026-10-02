import { describe, expect, it } from 'vitest'
import { navGroups } from './navigation'

describe('dashboard navigation', () => {
  it('puts guided startup first and groups existing tools under Build and Operate', () => {
    expect(navGroups.map((group) => group.label)).toEqual(['Start here', 'Build', 'Operate'])
    expect(navGroups[0].items.map((item) => item[2])).toEqual(['Getting started', 'System overview'])

    const sections = navGroups.flatMap((group) => group.items.map((item) => item[0]))
    expect(sections).toEqual(expect.arrayContaining([
      'agentlab', 'chat', 'speech', 'voice', 'vision', 'studio',
      'setup', 'overview', 'models', 'jobs', 'logs', 'health', 'network', 'maintenance', 'system',
    ]))
    expect(new Set(sections).size).toBe(sections.length)
  })
})
