const BAD_STATES = new Set(['failed', 'error', 'dead', 'oom', 'fail'])

export function isBadState(state?: string | null) {
  return !!state && BAD_STATES.has(state)
}
