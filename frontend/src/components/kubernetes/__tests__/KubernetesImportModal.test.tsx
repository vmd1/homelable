import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { KubernetesImportModal } from '../KubernetesImportModal'

vi.mock('@/api/client', () => ({
  kubernetesApi: {
    testConnection: vi.fn(),
    importNetwork: vi.fn(),
    importToPending: vi.fn(),
  },
}))
vi.mock('sonner', async () => (await import('@/test/mocks')).mockSonner())

import { kubernetesApi } from '@/api/client'
import { toast } from 'sonner'

const defaultProps = {
  open: true,
  onClose: vi.fn(),
  onAddToCanvas: vi.fn(),
  onInventoryImported: vi.fn(),
}

const URL_INPUT = 'https://192.168.1.10:6443'

const sampleNodes = [
  {
    id: 'k8s-abcd-node-cp1', label: 'cp1', type: 'server' as const,
    ieee_address: 'k8s-abcd-node-cp1', hostname: 'cp1', ip: '10.0.0.1', status: 'online',
    vendor: 'Kubernetes', model: 'arm64', parent_ieee: null,
  },
  {
    id: 'k8s-abcd-wl-apps-deployment-web', label: 'web', type: 'docker_container' as const,
    ieee_address: 'k8s-abcd-wl-apps-deployment-web', hostname: 'web', ip: null, status: 'online',
    vendor: 'Kubernetes', model: 'img/web:1', parent_ieee: 'k8s-abcd-node-cp1',
  },
]
const sampleEdges = [{ source: 'k8s-abcd-node-cp1', target: 'k8s-abcd-wl-apps-deployment-web' }]

function fillUrl() {
  fireEvent.change(screen.getByPlaceholderText(URL_INPUT), { target: { value: 'https://k8s.lan:6443' } })
}

describe('KubernetesImportModal', () => {
  beforeEach(() => {
    vi.mocked(kubernetesApi.testConnection).mockReset()
    vi.mocked(kubernetesApi.importNetwork).mockReset()
    vi.mocked(kubernetesApi.importToPending).mockReset()
    vi.mocked(kubernetesApi.importToPending).mockResolvedValue({ data: { id: 'run-1', status: 'running' } } as never)
    vi.mocked(kubernetesApi.importNetwork).mockResolvedValue({
      data: { nodes: sampleNodes, edges: sampleEdges, device_count: 2 },
    } as never)
    vi.mocked(toast.success).mockReset()
    vi.mocked(toast.error).mockReset()
    defaultProps.onClose.mockReset()
    defaultProps.onAddToCanvas.mockReset()
    defaultProps.onInventoryImported.mockReset()
  })

  it('renders a masked token input', () => {
    render(<KubernetesImportModal {...defaultProps} />)
    const token = document.querySelector('input[type="password"]')
    expect(token).not.toBeNull()
  })

  it('refuses to import without an API URL', () => {
    render(<KubernetesImportModal {...defaultProps} />)
    fireEvent.click(screen.getByRole('button', { name: /Import to Inventory/ }))
    expect(toast.error).toHaveBeenCalledWith('Enter the Kubernetes API server URL')
    expect(kubernetesApi.importToPending).not.toHaveBeenCalled()
  })

  it('sends the connection form, splitting namespaces, to the inventory import', async () => {
    render(<KubernetesImportModal {...defaultProps} />)
    fillUrl()
    fireEvent.change(screen.getByPlaceholderText('apps, media - blank for all'), { target: { value: 'apps, media ,' } })
    fireEvent.click(screen.getByRole('button', { name: /Import to Inventory/ }))
    await waitFor(() => expect(kubernetesApi.importToPending).toHaveBeenCalled())
    expect(vi.mocked(kubernetesApi.importToPending).mock.calls[0][0]).toEqual({
      api_url: 'https://k8s.lan:6443',
      token: undefined,
      verify_tls: true,
      namespaces: ['apps', 'media'],
      include_system: false,
      include_offline: true,
    })
    expect(defaultProps.onInventoryImported).toHaveBeenCalled()
    expect(defaultProps.onClose).toHaveBeenCalled()
  })

  it('reports a failed connection test with the server message', async () => {
    vi.mocked(kubernetesApi.testConnection).mockRejectedValue({ response: { data: { detail: 'Custom API needs a token' } } })
    render(<KubernetesImportModal {...defaultProps} />)
    fillUrl()
    fireEvent.click(screen.getByRole('button', { name: /Test Connection/ }))
    expect(await screen.findByText('Custom API needs a token')).toBeInTheDocument()
  })

  it('lists nodes and workloads and adds the selection to the canvas', async () => {
    render(<KubernetesImportModal {...defaultProps} />)
    fillUrl()
    fireEvent.click(screen.getByLabelText('Inventory + canvas'))
    fireEvent.click(screen.getByRole('button', { name: /Fetch Devices/ }))
    expect(await screen.findByText('cp1')).toBeInTheDocument()
    expect(screen.getByText('web')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Add 2 to Canvas/ }))
    expect(defaultProps.onAddToCanvas).toHaveBeenCalledWith(sampleNodes, sampleEdges)
  })

  it('drops edges whose end was unticked', async () => {
    render(<KubernetesImportModal {...defaultProps} />)
    fillUrl()
    fireEvent.click(screen.getByLabelText('Inventory + canvas'))
    fireEvent.click(screen.getByRole('button', { name: /Fetch Devices/ }))
    fireEvent.click(await screen.findByText('web'))
    fireEvent.click(screen.getByRole('button', { name: /Add 1 to Canvas/ }))
    expect(defaultProps.onAddToCanvas).toHaveBeenCalledWith([sampleNodes[0]], [])
  })
})
