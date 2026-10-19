import { useState } from 'react'
import { Server, Container, Boxes, CheckCircle2, XCircle, Loader2, Plus } from 'lucide-react'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { kubernetesApi, type KubernetesConnection } from '@/api/client'
import { toast } from 'sonner'
import type { KubernetesNode, KubernetesEdge, KubernetesNodeType } from './types'

interface KubernetesImportModalProps {
  open: boolean
  onClose: () => void
  onAddToCanvas: (nodes: KubernetesNode[], edges: KubernetesEdge[]) => void
  onInventoryImported?: () => void
}

type ImportMode = 'pending' | 'canvas'

const ACCENT = '#326ce5'

interface ConnectionForm {
  api_url: string
  token: string
  verify_tls: boolean
  /** Comma-separated; blank = every namespace but the system ones. */
  namespaces: string
  include_system: boolean
}

const DEFAULT_FORM: ConnectionForm = {
  api_url: '',
  token: '',
  verify_tls: true,
  namespaces: '',
  include_system: false,
}

const DEVICE_TYPE_ICON: Record<KubernetesNodeType, typeof Server> = {
  server: Server,
  docker_container: Container,
}

const DEVICE_TYPE_LABEL: Record<KubernetesNodeType, string> = {
  server: 'Cluster nodes',
  docker_container: 'Workloads',
}

const DEVICE_TYPE_COLOR: Record<KubernetesNodeType, string> = {
  server: ACCENT,
  docker_container: '#00d4ff',
}

function plural(n: number, noun: string): string {
  return `${n} ${noun}${n !== 1 ? 's' : ''}`
}

export function KubernetesImportModal({ open, onClose, onAddToCanvas, onInventoryImported }: KubernetesImportModalProps) {
  const [form, setForm] = useState<ConnectionForm>(DEFAULT_FORM)
  const [connectionStatus, setConnectionStatus] = useState<'idle' | 'testing' | 'ok' | 'fail'>('idle')
  const [connectionMsg, setConnectionMsg] = useState('')
  const [loading, setLoading] = useState(false)
  const [devices, setDevices] = useState<KubernetesNode[]>([])
  const [edges, setEdges] = useState<KubernetesEdge[]>([])
  const [checked, setChecked] = useState<Set<string>>(new Set())
  const [importMode, setImportMode] = useState<ImportMode>('pending')
  // Same meaning in both modes: NotReady nodes and not-ready workloads are imported and listed, or neither.
  const [includeOffline, setIncludeOffline] = useState(true)

  const updateField = (field: 'api_url' | 'token' | 'namespaces', value: string) =>
    setForm((f) => ({ ...f, [field]: value }))

  const buildPayload = (): KubernetesConnection => ({
    api_url: form.api_url.trim(),
    token: form.token.trim() || undefined,
    verify_tls: form.verify_tls,
    namespaces: form.namespaces.split(',').map((n) => n.trim()).filter(Boolean),
    include_system: form.include_system,
    include_offline: includeOffline,
  })

  const extractError = (err: unknown): string | undefined => {
    if (err && typeof err === 'object' && 'response' in err) {
      return (err as { response?: { data?: { detail?: string } } }).response?.data?.detail
    }
    return undefined
  }

  const handleTestConnection = async () => {
    if (!form.api_url.trim()) { toast.error('Enter the Kubernetes API server URL'); return }
    setConnectionStatus('testing')
    try {
      const res = await kubernetesApi.testConnection(buildPayload())
      setConnectionStatus(res.data.connected ? 'ok' : 'fail')
      setConnectionMsg(res.data.message)
    } catch (err) {
      setConnectionStatus('fail')
      setConnectionMsg(extractError(err) ?? 'Request failed - check the API server URL')
    }
  }

  const handleFetchDevices = async () => {
    if (!form.api_url.trim()) { toast.error('Enter the Kubernetes API server URL'); return }
    setLoading(true)
    try {
      if (importMode === 'pending') {
        await kubernetesApi.importToPending(buildPayload())
        onInventoryImported?.()
        handleClose()
      } else {
        const res = await kubernetesApi.importNetwork(buildPayload())
        setDevices(res.data.nodes)
        setEdges(res.data.edges)
        setChecked(new Set(res.data.nodes.map((n) => n.id)))
        const count = (type: KubernetesNodeType, noun: string) =>
          plural(res.data.nodes.filter((d) => d.type === type).length, noun)
        if (res.data.nodes.length === 0) {
          toast.info('No nodes or workloads found')
        } else {
          toast.success(`Found ${count('server', 'node')} and ${count('docker_container', 'workload')}`)
        }
        if (res.data.notice) toast.warning(res.data.notice)
      }
    } catch (err: unknown) {
      toast.error(extractError(err) ?? 'Failed to fetch Kubernetes devices')
    } finally {
      setLoading(false)
    }
  }

  const toggleCheck = (id: string) =>
    setChecked((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id); else next.add(id)
      return next
    })

  const toggleAll = () => {
    setChecked(checked.size === devices.length ? new Set() : new Set(devices.map((d) => d.id)))
  }

  const handleAddToCanvas = () => {
    const selectedDevices = devices.filter((d) => checked.has(d.id))
    const selectedIds = new Set(selectedDevices.map((d) => d.id))
    const selectedEdges = edges.filter((e) => selectedIds.has(e.source) && selectedIds.has(e.target))
    onAddToCanvas(selectedDevices, selectedEdges)
    toast.success(`Added ${selectedDevices.length} device${selectedDevices.length !== 1 ? 's' : ''} to canvas`)
    handleClose()
  }

  const handleClose = () => {
    setDevices([])
    setEdges([])
    setChecked(new Set())
    setConnectionStatus('idle')
    setConnectionMsg('')
    setImportMode('pending')
    setIncludeOffline(true)
    onClose()
  }

  const groupedDevices: Record<KubernetesNodeType, KubernetesNode[]> = {
    server: devices.filter((d) => d.type === 'server'),
    docker_container: devices.filter((d) => d.type === 'docker_container'),
  }

  return (
    <Dialog open={open} onOpenChange={(v) => !v && handleClose()}>
      <DialogContent className="bg-[#161b22] border-border max-w-2xl max-h-[85vh] flex flex-col">
        <DialogHeader>
          <DialogTitle className="text-foreground flex items-center gap-2">
            <Boxes size={16} style={{ color: ACCENT }} />
            Kubernetes Import
          </DialogTitle>
        </DialogHeader>

        <div className="flex-1 overflow-y-auto space-y-4 py-2 min-h-0">
          <div className="space-y-3">
            <div className="grid grid-cols-2 gap-x-4 gap-y-3">
              <div className="col-span-2 space-y-1">
                <Label className="text-xs text-muted-foreground">API Server URL</Label>
                <Input
                  value={form.api_url}
                  onChange={(e) => updateField('api_url', e.target.value)}
                  placeholder="https://192.168.1.10:6443"
                  className="font-mono text-sm bg-[#0d1117] border-border"
                />
              </div>
              <div className="col-span-2 space-y-1">
                <Label className="text-xs text-muted-foreground">ServiceAccount Token</Label>
                <Input
                  value={form.token}
                  onChange={(e) => updateField('token', e.target.value)}
                  type="password"
                  autoComplete="new-password"
                  className="text-sm bg-[#0d1117] border-border"
                />
              </div>
              <div className="col-span-2 space-y-1">
                <Label className="text-xs text-muted-foreground">Namespaces (optional)</Label>
                <Input
                  value={form.namespaces}
                  onChange={(e) => updateField('namespaces', e.target.value)}
                  placeholder="apps, media - blank for all"
                  className="font-mono text-sm bg-[#0d1117] border-border"
                />
              </div>
              <div className="col-span-2 flex flex-wrap items-center gap-x-6 gap-y-2 pt-1">
                <label className="flex items-center gap-1.5 text-xs text-muted-foreground cursor-pointer">
                  <input
                    type="checkbox"
                    checked={form.verify_tls}
                    onChange={(e) => setForm((f) => ({ ...f, verify_tls: e.target.checked }))}
                    className="w-3 h-3 cursor-pointer"
                    style={{ accentColor: ACCENT }}
                  />
                  Verify TLS certificate
                </label>
                <label className="flex items-center gap-1.5 text-xs text-muted-foreground cursor-pointer">
                  <input
                    type="checkbox"
                    checked={form.include_system}
                    onChange={(e) => setForm((f) => ({ ...f, include_system: e.target.checked }))}
                    className="w-3 h-3 cursor-pointer"
                    style={{ accentColor: ACCENT }}
                  />
                  Include system namespaces
                </label>
              </div>
              {!form.verify_tls && (
                <p className="col-span-2 text-[11px] text-[#e3b341]">
                  Without certificate verification the token can be intercepted on the network. Most clusters use a
                  private CA: set KUBERNETES_CA_FILE on the server instead of disabling verification.
                </p>
              )}
            </div>

            {connectionStatus !== 'idle' && (
              <div className={`flex items-center gap-1.5 text-xs px-2 py-1.5 rounded-md border ${
                connectionStatus === 'ok'
                  ? 'bg-[#39d353]/10 border-[#39d353]/30 text-[#39d353]'
                  : connectionStatus === 'fail'
                  ? 'bg-[#f85149]/10 border-[#f85149]/30 text-[#f85149]'
                  : 'bg-[#e3b341]/10 border-[#e3b341]/30 text-[#e3b341]'
              }`}>
                {connectionStatus === 'testing' && <Loader2 size={12} className="animate-spin" />}
                {connectionStatus === 'ok' && <CheckCircle2 size={12} />}
                {connectionStatus === 'fail' && <XCircle size={12} />}
                <span>{connectionStatus === 'testing' ? 'Testing...' : connectionMsg}</span>
              </div>
            )}

            <div className="space-y-2 rounded-md border border-border bg-[#0d1117]/60 px-3 py-2.5">
              <span className="block text-xs text-muted-foreground">Send devices to</span>
              <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-xs">
                <label className="flex items-center gap-1.5 cursor-pointer text-foreground">
                  <input
                    type="radio"
                    name="kubernetes-import-mode"
                    checked={importMode === 'pending'}
                    onChange={() => setImportMode('pending')}
                    className="cursor-pointer"
                    style={{ accentColor: ACCENT }}
                  />
                  Device inventory only
                </label>
                <label className="flex items-center gap-1.5 cursor-pointer text-foreground">
                  <input
                    type="radio"
                    name="kubernetes-import-mode"
                    checked={importMode === 'canvas'}
                    onChange={() => setImportMode('canvas')}
                    className="cursor-pointer"
                    style={{ accentColor: ACCENT }}
                  />
                  Inventory + canvas
                </label>
              </div>
            </div>

            <label className="flex items-center gap-2 rounded-md border border-border bg-[#0d1117]/60 px-3 py-2.5 text-xs cursor-pointer text-foreground">
              <input
                type="checkbox"
                checked={includeOffline}
                onChange={(e) => setIncludeOffline(e.target.checked)}
                className="w-3 h-3 cursor-pointer"
                style={{ accentColor: ACCENT }}
              />
              Include NotReady nodes and not-ready workloads
            </label>

            <div className="flex flex-wrap gap-2 pt-1">
              <Button
                size="sm"
                variant="ghost"
                className="gap-1.5 text-muted-foreground hover:text-foreground border border-border hover:bg-[#21262d]"
                onClick={handleTestConnection}
                disabled={connectionStatus === 'testing' || loading}
              >
                {connectionStatus === 'testing'
                  ? <Loader2 size={13} className="animate-spin" />
                  : <CheckCircle2 size={13} />}
                Test Connection
              </Button>
              <Button
                size="sm"
                style={{ background: ACCENT, color: '#ffffff' }}
                className="gap-1.5"
                onClick={handleFetchDevices}
                disabled={loading || connectionStatus === 'testing'}
              >
                {loading ? <Loader2 size={13} className="animate-spin" /> : <Boxes size={13} />}
                {importMode === 'pending' ? 'Import to Inventory' : 'Fetch Devices'}
              </Button>
            </div>
            <p className="text-[11px] text-muted-foreground italic">
              Leave the token blank to use the token configured on the server (.env), for the configured API URL only.
              Use a read-only ServiceAccount (a ClusterRole with get/list on nodes, pods, services, namespaces and
              apps deployments, statefulsets and daemonsets).
            </p>
          </div>

          {devices.length > 0 && (
            <div className="space-y-2">
              <div className="flex items-center gap-1.5">
                <input
                  type="checkbox"
                  checked={checked.size === devices.length}
                  ref={(el) => { if (el) el.indeterminate = checked.size > 0 && checked.size < devices.length }}
                  onChange={toggleAll}
                  className="w-3 h-3 cursor-pointer"
                  style={{ accentColor: ACCENT }}
                  title="Select all"
                />
                <span className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
                  Devices ({checked.size}/{devices.length} selected)
                </span>
              </div>

              {(Object.entries(groupedDevices) as [KubernetesNodeType, KubernetesNode[]][])
                .filter(([, group]) => group.length > 0)
                .map(([type, group]) => {
                  const Icon = DEVICE_TYPE_ICON[type]
                  const color = DEVICE_TYPE_COLOR[type]
                  return (
                    <div key={type}>
                      <div className="flex items-center gap-1.5 mb-1">
                        <Icon size={11} style={{ color }} />
                        <span className="text-[10px] font-medium uppercase tracking-wider" style={{ color }}>
                          {DEVICE_TYPE_LABEL[type]} ({group.length})
                        </span>
                      </div>
                      {group.map((device) => (
                        <div
                          key={device.id}
                          className={`flex items-start gap-2 p-2 mb-1 rounded-md text-xs cursor-pointer transition-colors border ${
                            checked.has(device.id)
                              ? 'bg-[#21262d] border-[#326ce5]/40'
                              : 'bg-[#21262d] border-transparent hover:bg-[#30363d]'
                          }`}
                          onClick={() => toggleCheck(device.id)}
                        >
                          <input
                            type="checkbox"
                            checked={checked.has(device.id)}
                            onChange={() => toggleCheck(device.id)}
                            onClick={(e) => e.stopPropagation()}
                            className="w-3 h-3 mt-0.5 cursor-pointer shrink-0"
                            style={{ accentColor: ACCENT }}
                          />
                          <div className="flex-1 min-w-0">
                            <div className="text-foreground font-medium truncate">{device.label}</div>
                            {device.ip && (
                              <div className="font-mono text-[10px] text-muted-foreground truncate">{device.ip}</div>
                            )}
                            <div className="text-[10px] text-muted-foreground truncate">
                              {[device.model, device.status].filter(Boolean).join(' - ')}
                            </div>
                          </div>
                        </div>
                      ))}
                    </div>
                  )
                })}
            </div>
          )}
        </div>

        <DialogFooter className="gap-2 shrink-0 pt-2 border-t border-border">
          <Button variant="ghost" onClick={handleClose}>Cancel</Button>
          {devices.length > 0 && (
            <Button
              onClick={handleAddToCanvas}
              disabled={checked.size === 0}
              style={{ background: ACCENT, color: '#ffffff' }}
              className="gap-1.5"
            >
              <Plus size={13} />
              Add {checked.size} to Canvas
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
