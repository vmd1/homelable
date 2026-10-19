/** Shared Kubernetes import type definitions for the frontend. */

import type { NodeProperty, ServiceInfo } from '@/types'

export type KubernetesNodeType = 'server' | 'docker_container'

export interface KubernetesNode {
  id: string
  label: string
  type: KubernetesNodeType
  ieee_address: string
  hostname?: string | null
  ip?: string | null
  status: string
  vendor?: string | null
  /** Image for a workload, CPU architecture for a cluster node. */
  model?: string | null
  parent_ieee?: string | null
  /** Device Inventory row this node draws - stamped by the import so the
   * canvas save links to it instead of minting a second row. */
  device_id?: string | null
  /** The inventory row's lists - a workload's Kubernetes Services are services. */
  services?: ServiceInfo[]
  properties?: NodeProperty[]
}

export interface KubernetesEdge {
  source: string
  target: string
}

export interface KubernetesImportResponse {
  nodes: KubernetesNode[]
  edges: KubernetesEdge[]
  device_count: number
  /** Why part of the cluster was left out (e.g. the token cannot list pods). */
  notice?: string | null
}
