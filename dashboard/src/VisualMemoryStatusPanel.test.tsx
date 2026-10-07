import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { VisualMemoryStatusPanel } from './VisualMemoryStatusPanel'
import type { VisualMemoryStatus } from './api'

const mocks = vi.hoisted(() => ({ visualMemoryStatus: vi.fn() }))
vi.mock('./api', () => ({ localAI: { visualMemoryStatus: mocks.visualMemoryStatus } }))

const status: VisualMemoryStatus = {
  status: 'ok',
  service: 'visual-memory',
  model: 'google/embeddinggemma-2',
  revision: 'pinned-revision',
  dimension: 768,
  device: 'cpu',
  loaded: false,
  model_files_available: true,
  database: {
    available: true, writable: true, size_bytes: 2 * 1024 * 1024,
    records: 12, observations: 17, vectors: 64,
  },
  index: { compatible: true, backend: 'hnswlib', size_bytes: 1024 * 1024 },
  latency_percentiles_ms: {
    embedding: { p50: 12.5, p95: 24.1 },
    search: { p50: 2.2, p95: 5.4 },
  },
}

describe('VisualMemoryStatusPanel', () => {
  beforeEach(() => {
    mocks.visualMemoryStatus.mockReset().mockResolvedValue(status)
  })

  it('shows model provenance, storage/index readiness, counts, and latency percentiles', async () => {
    render(<VisualMemoryStatusPanel />)

    expect(await screen.findByText('google/embeddinggemma-2')).toBeInTheDocument()
    expect(screen.getByText('pinned-revision')).toBeInTheDocument()
    expect(screen.getByText('12 / 64')).toBeInTheDocument()
    expect(screen.getByText('compatible · hnswlib')).toBeInTheDocument()
    expect(screen.getByText('12.5 ms / 24.1 ms')).toBeInTheDocument()
    expect(screen.getByText('2.2 ms / 5.4 ms')).toBeInTheDocument()
    expect(mocks.visualMemoryStatus).toHaveBeenCalledTimes(1)
  })
})
