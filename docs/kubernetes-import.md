# Kubernetes Import

Connects Homelable to a Kubernetes cluster, reads its nodes and workloads over
the Kubernetes API, and brings them into the Device Inventory (and, optionally,
onto the canvas). It can also **sync** on a schedule.

> **Server-dependent feature** - requires the Homelable backend. It is hidden in
> the no-backend standalone/demo build.

## What is imported

- **Every cluster node** as a `server` node: InternalIP, hostname, CPU count,
  memory, and hidden properties (Cluster, Roles, Kubelet, Runtime, Kernel, OS,
  Architecture). A node whose `Ready` condition is not `True` is `offline`.
  A node is matched to a device a network scan already found by IP.
- **Every Deployment, StatefulSet and DaemonSet** as a `docker_container`
  node (Namespace, Kind, Replicas, Images as hidden properties). It is `online`
  when all desired replicas are ready, otherwise `offline` (scaled to zero
  counts as offline).
- **Node -> workload links** as `virtual` edges: each workload is linked from
  every node running one of its pods.
- **Kubernetes Services** that select a workload and are reachable from outside
  the cluster become service entries: the LoadBalancer IP (or an external IP),
  or a NodePort on a node running the workload. A ClusterIP is not reachable
  from Homelable, so it is not a service entry (a check against it would only
  fail); those Services are listed in a hidden **Internal Services** property.
  Re-imports refresh the address but keep a name or icon you gave it.

`kube-system`, `kube-public` and `kube-node-lease` are skipped unless you tick
**Include system namespaces** (or list them in the namespace allowlist).
Pods, ReplicaSets and Jobs are not imported as devices of their own.

Identity is the cluster key (the `kube-system` namespace UID) plus object name,
so re-imports update the same rows and two clusters never collide.

## Credentials

Create a read-only ServiceAccount with
[`examples/kubernetes-readonly-rbac.yaml`](examples/kubernetes-readonly-rbac.yaml).

**Homelable running as a pod in the cluster (recommended).** Run the backend with
that ServiceAccount (`serviceAccountName: homelable`) and set nothing else: the API
address (`https://kubernetes.default.svc`), the pod's own token and the cluster CA
are picked up from `/var/run/secrets/kubernetes.io/serviceaccount`. The token is
re-read on every use, so Kubernetes' automatic rotation needs no handling.

**Homelable outside the cluster, or a different cluster.** Set `KUBERNETES_API_URL`
and either `KUBERNETES_TOKEN`, or `KUBERNETES_TOKEN_FILE` (a file, re-read on every
use). For a short-lived token use `kubectl -n homelable create token homelable
--duration=8760h`.

The token is a credential: it is sent with a request, or read from the server
configuration, and is **never persisted or returned by the API**. Precedence is
`KUBERNETES_TOKEN`, then `KUBERNETES_TOKEN_FILE`, then the pod's own token. A token
left blank in the dialog falls back to the configured one, only for the configured
API address (an explicit `KUBERNETES_API_URL`, else the in-cluster one), only over
`https://`, and never with TLS verification off when the server keeps it on. The
pod's cluster CA is trusted only for the in-cluster address, never for an
explicit `KUBERNETES_API_URL`. Redirects are not followed.

If the token cannot list pods or services, workloads still import, without node
links or services, and the run carries a warning.

## Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `KUBERNETES_API_URL` | in-cluster default | API server, e.g. `https://192.168.1.10:6443` |
| `KUBERNETES_TOKEN` | | ServiceAccount token |
| `KUBERNETES_TOKEN_FILE` | pod's own token | File holding the token, re-read on every use |
| `KUBERNETES_VERIFY_TLS` | `true` | Verify the API server certificate |
| `KUBERNETES_CA_FILE` | pod's cluster CA (in-cluster only) | Cluster CA bundle for a private CA (preferred over disabling verification) |
| `KUBERNETES_NAMESPACES` | | Comma-separated allowlist; empty = all but system |
| `KUBERNETES_INCLUDE_SYSTEM` | `false` | Also import system namespaces when no allowlist is set |

Auto-sync (on/off, interval >= 300 s, include not-ready) is set under
**Settings > Kubernetes auto-sync**; "Re-sync now" runs one import with the
`.env` config. Runs show up in Scan History as `kubernetes`.

## API

`POST /api/v1/kubernetes/{test-connection,import,import-pending,sync-now,config}`,
`GET /api/v1/kubernetes/config`.
