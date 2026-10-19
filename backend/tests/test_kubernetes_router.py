"""API + persistence tests for /api/v1/kubernetes/*."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.api.routes.kubernetes import _persist_pending_import
from app.core.config import settings
from app.db.models import InventoryDevice, InventoryDeviceLink

NODE = "k8s-abcd-node-cp1"
WL = "k8s-abcd-wl-apps-deployment-web"
URL = "https://k8s.lan:6443"


@pytest.fixture(autouse=True)
def _clear_env():
    fields = ("kubernetes_api_url", "kubernetes_token", "kubernetes_verify_tls")
    saved = {f: getattr(settings, f) for f in fields}
    settings.kubernetes_api_url = ""
    settings.kubernetes_token = ""
    settings.kubernetes_verify_tls = True
    yield
    for field, value in saved.items():
        setattr(settings, field, value)


def _nodes(status: str = "online") -> tuple[list[dict], list[dict]]:
    node = {
        "id": NODE, "label": "cp1", "type": "server", "ieee_address": NODE, "hostname": "cp1",
        "ip": "10.0.0.1", "mac": None, "status": "online", "cpu_count": 4, "ram_gb": 8.0,
        "vendor": "Kubernetes", "model": "arm64", "os_version": "Ubuntu", "services": [], "parent_ieee": None,
    }
    wl = {
        "id": WL, "label": "web", "type": "docker_container", "ieee_address": WL, "hostname": "web",
        "ip": None, "mac": None, "status": status, "vendor": "Kubernetes", "model": "img/web:1",
        "namespace": "apps", "workload_kind": "Deployment", "services": [], "parent_ieee": NODE,
    }
    return [node, wl], [{"source": NODE, "target": WL}]


@pytest.mark.asyncio
async def test_custom_api_without_token_is_rejected(client: AsyncClient, headers: dict) -> None:
    settings.kubernetes_api_url = URL
    settings.kubernetes_token = "envtok"
    res = await client.post("/api/v1/kubernetes/test-connection", json={"api_url": "https://evil:6443"}, headers=headers)
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_env_token_used_for_configured_api(client: AsyncClient, headers: dict) -> None:
    settings.kubernetes_api_url = URL
    settings.kubernetes_token = "envtok"
    with patch("app.api.routes.kubernetes.test_kubernetes_connection", new=AsyncMock(return_value=(True, "ok"))) as t:
        res = await client.post("/api/v1/kubernetes/test-connection", json={"api_url": "K8S.lan:6443"}, headers=headers)
    assert res.status_code == 200 and t.await_args.kwargs["token"] == "envtok"


@pytest.mark.asyncio
async def test_env_token_refused_over_plain_http_or_without_tls_verify(client: AsyncClient, headers: dict) -> None:
    settings.kubernetes_api_url = "http://k8s.lan:8080"
    settings.kubernetes_token = "envtok"
    res = await client.post("/api/v1/kubernetes/test-connection", json={"api_url": "http://k8s.lan:8080"}, headers=headers)
    assert res.status_code == 400
    settings.kubernetes_api_url = URL
    res = await client.post(
        "/api/v1/kubernetes/test-connection", json={"api_url": URL, "verify_tls": False}, headers=headers
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_import_returns_nodes_and_records_inventory(client: AsyncClient, headers: dict) -> None:
    nodes, edges = _nodes()
    with patch(
        "app.api.routes.kubernetes.fetch_kubernetes_inventory", new=AsyncMock(return_value=(nodes, edges, None))
    ):
        res = await client.post("/api/v1/kubernetes/import", json={"api_url": URL, "token": "t"}, headers=headers)
    assert res.status_code == 200
    body = res.json()
    assert body["device_count"] == 2 and body["edges"] == [{"source": NODE, "target": WL}]
    assert all(n["device_id"] for n in body["nodes"])
    assert "token" not in res.text


@pytest.mark.asyncio
async def test_import_offline_excluded_when_unwanted(client: AsyncClient, headers: dict) -> None:
    nodes, edges = _nodes(status="offline")
    with patch(
        "app.api.routes.kubernetes.fetch_kubernetes_inventory", new=AsyncMock(return_value=(nodes, edges, None))
    ):
        res = await client.post(
            "/api/v1/kubernetes/import",
            json={"api_url": URL, "token": "t", "include_offline": False},
            headers=headers,
        )
    assert [n["id"] for n in res.json()["nodes"]] == [NODE]
    assert res.json()["edges"] == []


@pytest.mark.asyncio
async def test_connection_errors_map_to_502(client: AsyncClient, headers: dict) -> None:
    with patch(
        "app.api.routes.kubernetes.fetch_kubernetes_inventory", new=AsyncMock(side_effect=ConnectionError("nope"))
    ):
        res = await client.post("/api/v1/kubernetes/import", json={"api_url": URL, "token": "t"}, headers=headers)
    assert res.status_code == 502 and res.json()["detail"] == "nope"


@pytest.mark.asyncio
async def test_persist_is_idempotent_and_replaces_links(db_session) -> None:
    nodes, edges = _nodes()
    first = await _persist_pending_import(db_session, nodes, edges)
    second = await _persist_pending_import(db_session, nodes, edges)
    assert (first.pending_created, second.pending_created, second.pending_updated) == (2, 0, 2)
    rows = (await db_session.execute(select(InventoryDevice))).scalars().all()
    assert {r.ieee_address for r in rows} == {NODE, WL}
    assert all(r.discovery_source == "kubernetes" for r in rows)
    links = (await db_session.execute(select(InventoryDeviceLink))).scalars().all()
    assert len(links) == 1 and links[0].discovery_source == "kubernetes"


@pytest.mark.asyncio
async def test_config_never_exposes_token_and_sync_needs_env(client: AsyncClient, headers: dict) -> None:
    settings.kubernetes_api_url = URL
    settings.kubernetes_token = "envtok"
    res = await client.get("/api/v1/kubernetes/config", headers=headers)
    assert res.json()["token_configured"] is True and "envtok" not in res.text
    settings.kubernetes_token = ""
    res = await client.post("/api/v1/kubernetes/config", json={"sync_enabled": True}, headers=headers)
    assert res.status_code == 400
