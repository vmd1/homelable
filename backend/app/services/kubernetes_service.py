"""Kubernetes inventory service: fetch cluster nodes and their workloads.

Talks to the Kubernetes API server over REST with a bearer token (a
ServiceAccount token with read-only access: get/list on nodes, pods, services,
namespaces and apps/{deployments,statefulsets,daemonsets}). No kubernetes client
library is needed. Returns plain homelable node dicts + node->workload edge
hints; DB persistence lives in the route layer (``app.api.routes.kubernetes``).

Identity is the cluster key (the ``kube-system`` namespace UID, which survives
renames and re-addressing) plus the object's name, so a re-import updates the
same Device Inventory rows. A cluster node becomes a ``server``; a Deployment,
StatefulSet or DaemonSet becomes a ``docker_container`` linked from every node
that runs one of its pods. Kubernetes Services are attached to the workloads
they select as service entries (LoadBalancer IP / NodePort / ClusterIP).
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from app.services.zigbee_service import merge_zigbee_properties

logger = logging.getLogger(__name__)

# Same NodeProperty shape + visibility-preservation rules as the other importers.
merge_kubernetes_properties = merge_zigbee_properties

_CONNECT_TIMEOUT = 8.0
_READ_TIMEOUT = 30.0
_PAGE_SIZE = 500
_MAX_PAGES = 100

_WORKLOAD_KINDS = {
    "deployments": "Deployment",
    "statefulsets": "StatefulSet",
    "daemonsets": "DaemonSet",
}

# Namespaces holding the control plane and add-ons, skipped unless asked for.
SYSTEM_NAMESPACES = {"kube-system", "kube-public", "kube-node-lease"}


class KubernetesAuthError(ConnectionError):
    """The API server rejected the token (expired, or missing RBAC permissions)."""


def _sanitize_kubernetes_error(exc: BaseException) -> str:
    """Return a generic, credential-free message for a Kubernetes/HTTP error."""
    logger.warning("Kubernetes error (sanitized for client): %r", exc)
    if isinstance(exc, KubernetesAuthError):
        return str(exc)
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 401:
            return "Authentication failed - the token is invalid or expired"
        if code == 403:
            return "The token lacks permission - it needs read access to nodes, pods, services and apps workloads"
        if code == 404:
            return "Kubernetes API not found - check the API server URL"
        return f"Kubernetes API returned HTTP {code}"
    raw = str(exc).lower()
    if "name or service not known" in raw or "getaddrinfo" in raw or "nodename nor servname" in raw:
        return "Kubernetes API host could not be resolved"
    if "refused" in raw:
        return "Connection refused by the Kubernetes API server"
    if "certificate" in raw or "ssl" in raw or "tls" in raw:
        return "TLS verification failed - untick 'Verify TLS certificate' or provide the cluster CA"
    if "timed out" in raw or "timeout" in raw:
        return "Connection to the Kubernetes API server timed out"
    return "Kubernetes connection failed"


def normalize_api_url(url: str) -> str:
    """Accept ``host``, ``host:6443`` or a full URL; default to https."""
    url = url.strip().rstrip("/")
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = f"https://{url}"
    return url


def _client(api_url: str, token: str, verify: bool | str) -> httpx.AsyncClient:
    # Never follows redirects, so the bearer token is only ever sent to the
    # address the user configured.
    return httpx.AsyncClient(
        base_url=normalize_api_url(api_url),
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        verify=verify,
        timeout=httpx.Timeout(_READ_TIMEOUT, connect=_CONNECT_TIMEOUT),
    )


def _verify_setting(verify_tls: bool, ca_file: str | None) -> bool | str:
    """httpx ``verify`` value: a CA bundle path wins, else the plain flag."""
    if verify_tls and ca_file:
        return ca_file
    return verify_tls


async def _get_json(client: httpx.AsyncClient, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    resp = await client.get(path, params=params)
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict):
        raise ValueError("Malformed Kubernetes API response")
    return payload


async def _list_items(client: httpx.AsyncClient, path: str) -> list[dict[str, Any]]:
    """All items of a list endpoint, following ``continue`` tokens."""
    items: list[dict[str, Any]] = []
    token: str | None = None
    for _ in range(_MAX_PAGES):
        params: dict[str, Any] = {"limit": _PAGE_SIZE}
        if token:
            params["continue"] = token
        payload = await _get_json(client, path, params)
        items.extend(i for i in payload.get("items") or [] if isinstance(i, dict))
        token = (payload.get("metadata") or {}).get("continue") or None
        if not token:
            break
    return items


def _cpu_count(value: Any) -> int | None:
    """Parse a CPU quantity: ``"4"`` or ``"3920m"`` (millicores, rounded down)."""
    try:
        text = str(value)
        return int(text[:-1]) // 1000 if text.endswith("m") else int(float(text))
    except (TypeError, ValueError):
        return None


def _memory_gb(value: Any) -> float | None:
    """Parse a memory quantity (``16384Mi``, ``8Gi``, ``8000000Ki``) into GB."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([KMGT]i?)?", str(value or ""))
    if not match:
        return None
    number, unit = float(match.group(1)), match.group(2) or ""
    factors = {"": 1, "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
               "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4}
    return round(number * factors[unit] / 1024**3, 1)


def _node_ready(node: dict[str, Any]) -> bool:
    for cond in (node.get("status") or {}).get("conditions") or []:
        if isinstance(cond, dict) and cond.get("type") == "Ready":
            return cond.get("status") == "True"
    return False


def _node_address(node: dict[str, Any], kind: str) -> str | None:
    for addr in (node.get("status") or {}).get("addresses") or []:
        if isinstance(addr, dict) and addr.get("type") == kind and addr.get("address"):
            return str(addr["address"])
    return None


def _node_roles(node: dict[str, Any]) -> list[str]:
    prefix = "node-role.kubernetes.io/"
    labels = (node.get("metadata") or {}).get("labels") or {}
    return sorted(k[len(prefix):] or "worker" for k in labels if k.startswith(prefix))


def _k8s_node(raw: dict[str, Any], cluster_key: str, cluster_name: str | None) -> dict[str, Any] | None:
    meta = raw.get("metadata") or {}
    name = meta.get("name")
    if not name:
        return None
    status = raw.get("status") or {}
    info = status.get("nodeInfo") or {}
    capacity = status.get("capacity") or {}
    roles = _node_roles(raw)
    return {
        "id": f"k8s-{cluster_key}-node-{name}",
        "label": name,
        "type": "server",
        "ieee_address": f"k8s-{cluster_key}-node-{name}",
        "hostname": _node_address(raw, "Hostname") or name,
        "ip": _node_address(raw, "InternalIP"),
        "mac": None,
        "status": "online" if _node_ready(raw) else "offline",
        "cpu_count": _cpu_count(capacity.get("cpu")),
        "ram_gb": _memory_gb(capacity.get("memory")),
        "vendor": "Kubernetes",
        "model": info.get("architecture") or None,
        "os_version": info.get("osImage") or None,
        "kubelet_version": info.get("kubeletVersion") or None,
        "container_runtime": info.get("containerRuntimeVersion") or None,
        "kernel_version": info.get("kernelVersion") or None,
        "roles": roles,
        "cluster": cluster_name,
        "services": [],
        "parent_ieee": None,
    }


def _selector_matches(selector: dict[str, str], labels: dict[str, str]) -> bool:
    return bool(selector) and all(labels.get(k) == v for k, v in selector.items())


def _service_entries(svc: dict[str, Any]) -> list[dict[str, Any]]:
    """Service entries (ip/port) for one Kubernetes Service, most reachable address first."""
    spec = svc.get("spec") or {}
    meta = svc.get("metadata") or {}
    name = meta.get("name") or "service"
    svc_type = spec.get("type") or "ClusterIP"
    lb = [
        i.get("ip") or i.get("hostname")
        for i in ((svc.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
        if isinstance(i, dict)
    ]
    external = [str(a) for a in spec.get("externalIPs") or []]
    cluster_ip = spec.get("clusterIP")
    host = next((a for a in [*lb, *external] if a), None)
    entries: list[dict[str, Any]] = []
    for p in spec.get("ports") or []:
        if not isinstance(p, dict) or not p.get("port"):
            continue
        port_name = p.get("name")
        label = f"{name} ({port_name})" if port_name and len(spec.get("ports") or []) > 1 else name
        entry: dict[str, Any] = {
            "port": int(p["port"]),
            "protocol": str(p.get("protocol") or "TCP").lower(),
            "service_name": label,
            "path": "",
            "notes": f"Kubernetes {svc_type}",
        }
        if host:
            entry["host"] = host
        elif cluster_ip and cluster_ip != "None":
            entry["host"] = cluster_ip
        if svc_type == "NodePort" and p.get("nodePort"):
            entry["notes"] = f"Kubernetes NodePort {p['nodePort']}"
        entries.append(entry)
    return entries


def _workload_status(kind: str, raw: dict[str, Any]) -> str:
    spec = raw.get("spec") or {}
    status = raw.get("status") or {}
    if kind == "DaemonSet":
        desired = status.get("desiredNumberScheduled") or 0
        ready = status.get("numberReady") or 0
    else:
        desired = spec.get("replicas", 1)
        ready = status.get("readyReplicas") or 0
    if not desired:
        return "offline"
    return "online" if ready >= desired else "offline"


def _workload_node(
    kind: str,
    raw: dict[str, Any],
    cluster_key: str,
    services: list[dict[str, Any]],
) -> dict[str, Any] | None:
    meta = raw.get("metadata") or {}
    name, ns = meta.get("name"), meta.get("namespace")
    if not name or not ns:
        return None
    template_labels = (((raw.get("spec") or {}).get("template") or {}).get("metadata") or {}).get("labels") or {}
    entries: list[dict[str, Any]] = []
    for svc in services:
        if (svc.get("metadata") or {}).get("namespace") != ns:
            continue
        selector = (svc.get("spec") or {}).get("selector") or {}
        if _selector_matches(selector, template_labels):
            entries.extend(_service_entries(svc))
    containers = (((raw.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []
    images = [c.get("image") for c in containers if isinstance(c, dict) and c.get("image")]
    ieee = f"k8s-{cluster_key}-wl-{ns}-{kind.lower()}-{name}"
    return {
        "id": ieee,
        "label": name,
        "type": "docker_container",
        "ieee_address": ieee,
        "hostname": name,
        "ip": None,
        "mac": None,
        "status": _workload_status(kind, raw),
        "vendor": "Kubernetes",
        "model": images[0] if images else None,
        "images": images,
        "namespace": ns,
        "workload_kind": kind,
        "replicas": (raw.get("spec") or {}).get("replicas"),
        "services": entries,
        "parent_ieee": None,
    }


def _pod_workload_key(pod: dict[str, Any]) -> tuple[str, str, str] | None:
    """(namespace, kind, name) of the workload that owns a pod, or None.

    A Deployment's pods are owned by a ReplicaSet named ``<deployment>-<hash>``.
    """
    meta = pod.get("metadata") or {}
    ns = meta.get("namespace")
    for owner in meta.get("ownerReferences") or []:
        if not isinstance(owner, dict) or not ns:
            continue
        kind, name = owner.get("kind"), owner.get("name")
        if kind == "ReplicaSet" and name:
            return ns, "Deployment", name.rsplit("-", 1)[0]
        if kind in ("StatefulSet", "DaemonSet") and name:
            return ns, kind, name
    return None


def _parse_inventory(
    cluster_key: str,
    cluster_name: str | None,
    raw_nodes: list[dict[str, Any]],
    raw_workloads: dict[str, list[dict[str, Any]]],
    pods: list[dict[str, Any]],
    services: list[dict[str, Any]],
    namespaces: set[str] | None = None,
    include_system: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    seen: set[str] = set()
    node_ids: dict[str, str] = {}
    for raw in raw_nodes:
        node = _k8s_node(raw, cluster_key, cluster_name)
        if node is None or node["id"] in seen:
            continue
        seen.add(node["id"])
        nodes.append(node)
        node_ids[node["label"]] = node["id"]

    def wanted(ns: str) -> bool:
        if namespaces is not None:
            return ns in namespaces
        return include_system or ns not in SYSTEM_NAMESPACES

    # workload key -> names of the nodes running at least one of its pods
    placements: dict[tuple[str, str, str], set[str]] = {}
    for pod in pods:
        key = _pod_workload_key(pod)
        node_name = (pod.get("spec") or {}).get("nodeName")
        phase = (pod.get("status") or {}).get("phase")
        if key and node_name and phase in ("Running", "Pending"):
            placements.setdefault(key, set()).add(node_name)

    for plural, kind in _WORKLOAD_KINDS.items():
        for raw in raw_workloads.get(plural, []):
            ns = (raw.get("metadata") or {}).get("namespace") or ""
            if not wanted(ns):
                continue
            workload = _workload_node(kind, raw, cluster_key, services)
            if workload is None or workload["id"] in seen:
                continue
            seen.add(workload["id"])
            nodes.append(workload)
            key = (ns, kind, workload["label"])
            parents = sorted(node_ids[n] for n in placements.get(key, ()) if n in node_ids)
            workload["parent_ieee"] = parents[0] if parents else None
            edges.extend({"source": p, "target": workload["id"]} for p in parents)
    return nodes, edges


def build_kubernetes_properties(node: dict[str, Any]) -> list[dict[str, Any]]:
    """NodeProperty list for a Kubernetes node or workload. All hidden by default."""
    props: list[dict[str, Any]] = []

    def add(key: str, value: Any, icon: str | None = None) -> None:
        if value:
            props.append({"key": key, "value": str(value), "icon": icon, "visible": False})

    if node.get("type") == "server":
        add("Cluster", node.get("cluster"))
        add("Roles", ", ".join(node.get("roles") or []))
        add("Kubelet", node.get("kubelet_version"))
        add("Runtime", node.get("container_runtime"))
        add("Kernel", node.get("kernel_version"))
        add("OS", node.get("os_version"))
        add("Architecture", node.get("model"))
        add("Memory", f"{node['ram_gb']} GB" if node.get("ram_gb") else None, "MemoryStick")
    else:
        add("Namespace", node.get("namespace"))
        add("Kind", node.get("workload_kind"))
        add("Replicas", node.get("replicas"))
        add("Images", ", ".join(node.get("images") or []))
    props.append({"key": "Source", "value": "Kubernetes", "icon": None, "visible": False})
    return props


async def _cluster_identity(client: httpx.AsyncClient, api_url: str) -> tuple[str, str]:
    """(stable cluster key, display name). The kube-system UID is stable for the
    cluster's lifetime; without access to it the API address is the key."""
    try:
        ns = await _get_json(client, "/api/v1/namespaces/kube-system")
        uid = (ns.get("metadata") or {}).get("uid")
    except (httpx.HTTPError, ValueError):
        uid = None
    host = normalize_api_url(api_url).split("://", 1)[1]
    key = str(uid)[:8] if uid else re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")
    return key, host


async def fetch_kubernetes_inventory(
    api_url: str,
    token: str,
    verify_tls: bool = True,
    ca_file: str | None = None,
    namespaces: list[str] | None = None,
    include_system: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """Fetch the cluster's nodes and workloads: (nodes, edges, notice).

    ``notice`` says why part of the cluster was left out, when it was.

    Raises:
        ConnectionError: transport/TLS/auth failures (sanitized message).
        ValueError: malformed API response.
    """
    notice: str | None = None
    try:
        async with _client(api_url, token, _verify_setting(verify_tls, ca_file)) as client:
            cluster_key, cluster_name = await _cluster_identity(client, api_url)
            raw_nodes = await _list_items(client, "/api/v1/nodes")
            workloads: dict[str, list[dict[str, Any]]] = {}
            for plural in _WORKLOAD_KINDS:
                workloads[plural] = await _list_items(client, f"/apis/apps/v1/{plural}")
            try:
                pods = await _list_items(client, "/api/v1/pods")
                services = await _list_items(client, "/api/v1/services")
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 403:
                    raise
                pods, services = [], []
                notice = (
                    "Workloads were imported without node placement or services: "
                    "the token cannot list pods/services"
                )
    except KubernetesAuthError:
        raise
    except httpx.HTTPError as exc:
        raise ConnectionError(_sanitize_kubernetes_error(exc)) from exc
    nodes, edges = _parse_inventory(
        cluster_key, cluster_name, raw_nodes, workloads, pods, services,
        set(namespaces) if namespaces else None, include_system,
    )
    return nodes, edges, notice


async def test_kubernetes_connection(
    api_url: str,
    token: str,
    verify_tls: bool = True,
    ca_file: str | None = None,
) -> tuple[bool, str]:
    """Quick reachability + auth check. Returns (connected, message)."""
    try:
        async with _client(api_url, token, _verify_setting(verify_tls, ca_file)) as client:
            version = await _get_json(client, "/version")
            # /version is readable unauthenticated on many clusters; listing nodes proves the token.
            nodes = await _get_json(client, "/api/v1/nodes", {"limit": 1})
    except (httpx.HTTPError, KubernetesAuthError) as exc:
        return False, _sanitize_kubernetes_error(exc)
    except Exception as exc:  # noqa: BLE001 - surface a safe message, log the rest
        logger.exception("Unexpected error during Kubernetes connection test")
        return False, _sanitize_kubernetes_error(exc)
    git = version.get("gitVersion") or "?"
    count = len(nodes.get("items") or [])
    return True, f"Connected to Kubernetes {git}" + ("" if count else " (no nodes visible to this token)")
