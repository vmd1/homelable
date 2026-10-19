"""Unit tests for the Kubernetes inventory service."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services import kubernetes_service as ks


def _node(name: str, ip: str, ready: bool = True, roles: tuple[str, ...] = ()) -> dict:
    return {
        "metadata": {"name": name, "labels": {f"node-role.kubernetes.io/{r}": "" for r in roles}},
        "status": {
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
            "addresses": [{"type": "InternalIP", "address": ip}, {"type": "Hostname", "address": name}],
            "capacity": {"cpu": "3920m", "memory": "8Gi"},
            "nodeInfo": {"architecture": "arm64", "osImage": "Ubuntu 24.04", "kubeletVersion": "v1.31.0"},
        },
    }


def _deploy(name: str, ns: str = "apps", replicas: int = 2, ready: int = 2) -> dict:
    return {
        "metadata": {"name": name, "namespace": ns},
        "spec": {
            "replicas": replicas,
            "template": {"metadata": {"labels": {"app": name}}, "spec": {"containers": [{"image": f"img/{name}:1"}]}},
        },
        "status": {"readyReplicas": ready},
    }


def _pod(owner_kind: str, owner: str, node: str, ns: str = "apps") -> dict:
    return {
        "metadata": {"namespace": ns, "ownerReferences": [{"kind": owner_kind, "name": owner}]},
        "spec": {"nodeName": node},
        "status": {"phase": "Running"},
    }


def _svc(name: str, selector: dict, ns: str = "apps", svc_type: str = "LoadBalancer") -> dict:
    return {
        "metadata": {"name": name, "namespace": ns},
        "spec": {"type": svc_type, "clusterIP": "10.96.0.5", "selector": selector, "ports": [{"port": 80, "protocol": "TCP"}]},
        "status": {"loadBalancer": {"ingress": [{"ip": "192.168.1.200"}]}},
    }


def test_cpu_and_memory_parsing() -> None:
    assert ks._cpu_count("3920m") == 3
    assert ks._cpu_count("4") == 4
    assert ks._cpu_count(None) is None
    assert ks._memory_gb("8Gi") == 8.0
    assert ks._memory_gb("16384Mi") == 16.0
    assert ks._memory_gb("garbage") is None


def test_normalize_api_url() -> None:
    assert ks.normalize_api_url("10.0.0.1:6443/") == "https://10.0.0.1:6443"
    assert ks.normalize_api_url("http://k:8080") == "http://k:8080"


def test_parse_inventory_nodes_workloads_and_edges() -> None:
    nodes, edges = ks._parse_inventory(
        "abcd1234", "k8s.lan",
        [_node("cp1", "10.0.0.1", roles=("control-plane",)), _node("w1", "10.0.0.2", ready=False)],
        {"deployments": [_deploy("web"), _deploy("kube-dns", ns="kube-system")], "statefulsets": [], "daemonsets": []},
        [_pod("ReplicaSet", "web-5d8f7", "cp1"), _pod("ReplicaSet", "web-5d8f7", "w1")],
        [_svc("web", {"app": "web"})],
    )
    by_label = {n["label"]: n for n in nodes}
    assert set(by_label) == {"cp1", "w1", "web"}  # kube-system skipped
    assert by_label["cp1"]["type"] == "server" and by_label["cp1"]["status"] == "online"
    assert by_label["cp1"]["cpu_count"] == 3 and by_label["cp1"]["roles"] == ["control-plane"]
    assert by_label["w1"]["status"] == "offline"
    web = by_label["web"]
    assert web["type"] == "docker_container" and web["status"] == "online"
    assert web["ieee_address"] == "k8s-abcd1234-wl-apps-deployment-web"
    assert web["services"][0]["host"] == "192.168.1.200"
    assert {(e["source"], e["target"]) for e in edges} == {
        ("k8s-abcd1234-node-cp1", web["id"]),
        ("k8s-abcd1234-node-w1", web["id"]),
    }


def test_namespace_allowlist_and_include_system() -> None:
    wl = {"deployments": [_deploy("a", ns="x"), _deploy("b", ns="kube-system")], "statefulsets": [], "daemonsets": []}
    only_x, _ = ks._parse_inventory("k", None, [], wl, [], [], namespaces={"x"})
    assert [n["label"] for n in only_x] == ["a"]
    with_sys, _ = ks._parse_inventory("k", None, [], wl, [], [], include_system=True)
    assert {n["label"] for n in with_sys} == {"a", "b"}


def test_workload_status() -> None:
    assert ks._workload_status("Deployment", _deploy("a", replicas=0, ready=0)) == "offline"
    assert ks._workload_status("Deployment", _deploy("a", replicas=3, ready=2)) == "offline"
    ds = {"spec": {}, "status": {"desiredNumberScheduled": 3, "numberReady": 3}}
    assert ks._workload_status("DaemonSet", ds) == "online"


def test_nodeport_service_entry() -> None:
    svc = _svc("web", {"app": "web"}, svc_type="NodePort")
    svc["status"] = {}
    svc["spec"]["ports"][0]["nodePort"] = 30080
    entry = ks._service_entries(svc)[0]
    assert entry["host"] == "10.96.0.5" and "30080" in entry["notes"]


def test_properties_hidden_and_sourced() -> None:
    nodes, _ = ks._parse_inventory(
        "k", "c", [_node("cp1", "10.0.0.1")],
        {"deployments": [_deploy("web")], "statefulsets": [], "daemonsets": []}, [], [],
    )
    for n in nodes:
        props = ks.build_kubernetes_properties(n)
        assert all(p["visible"] is False for p in props)
        assert props[-1] == {"key": "Source", "value": "Kubernetes", "icon": None, "visible": False}


def test_sanitized_errors_never_leak_token() -> None:
    req = httpx.Request("GET", "https://k/api")
    err = httpx.HTTPStatusError("boom Bearer sekret", request=req, response=httpx.Response(403, request=req))
    msg = ks._sanitize_kubernetes_error(err)
    assert "sekret" not in msg and "permission" in msg


@pytest.mark.asyncio
async def test_fetch_inventory_denied_pods_gives_notice() -> None:
    req = httpx.Request("GET", "https://k/api/v1/pods")
    denied = httpx.HTTPStatusError("denied", request=req, response=httpx.Response(403, request=req))

    async def fake_list(_client, path: str):
        if path.endswith("/pods"):
            raise denied
        if path.endswith("/nodes"):
            return [_node("cp1", "10.0.0.1")]
        if path.endswith("/deployments"):
            return [_deploy("web")]
        return []

    with patch.object(ks, "_list_items", side_effect=fake_list), patch.object(
        ks, "_cluster_identity", new=AsyncMock(return_value=("abcd1234", "k8s.lan"))
    ):
        nodes, edges, notice = await ks.fetch_kubernetes_inventory("k8s.lan:6443", "tok")
    assert {n["label"] for n in nodes} == {"cp1", "web"}
    assert edges == [] and notice and "pods" in notice


@pytest.mark.asyncio
async def test_connection_failure_message() -> None:
    with patch.object(ks, "_get_json", side_effect=httpx.ConnectError("connection refused")):
        ok, msg = await ks.test_kubernetes_connection("k8s.lan", "tok")
    assert ok is False and msg == "Connection refused by the Kubernetes API server"
