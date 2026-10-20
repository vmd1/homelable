"""Token-file and in-cluster resolution of the Kubernetes connection settings."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from app.core import config as config_module
from app.core.config import settings

FIELDS = ("kubernetes_api_url", "kubernetes_token", "kubernetes_token_file", "kubernetes_ca_file")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """No env config, and the pod's ServiceAccount directory points at an empty tmp dir."""
    saved = {f: getattr(settings, f) for f in FIELDS}
    for f in FIELDS:
        setattr(settings, f, "")
    sa = tmp_path / "sa"
    sa.mkdir()
    monkeypatch.setattr(config_module, "KUBERNETES_SA_DIR", str(sa))
    yield sa
    for f, v in saved.items():
        setattr(settings, f, v)


def test_nothing_configured_outside_a_cluster() -> None:
    assert settings.kubernetes_effective_api_url == ""
    assert settings.kubernetes_effective_token == ""
    assert settings.kubernetes_effective_ca_file == ""


def test_explicit_token_wins_over_file(tmp_path: Path) -> None:
    f = tmp_path / "tok"
    f.write_text("from-file\n")
    settings.kubernetes_token = "explicit"
    settings.kubernetes_token_file = str(f)
    assert settings.kubernetes_effective_token == "explicit"


def test_token_file_is_read_fresh_each_time_so_rotation_works(tmp_path: Path) -> None:
    f = tmp_path / "tok"
    f.write_text("first\n")
    settings.kubernetes_token_file = str(f)
    assert settings.kubernetes_effective_token == "first"
    f.write_text("  rotated \n")
    assert settings.kubernetes_effective_token == "rotated"


def test_missing_or_unreadable_token_file_is_not_configured(tmp_path: Path) -> None:
    settings.kubernetes_token_file = str(tmp_path / "nope")
    assert settings.kubernetes_effective_token == ""
    directory = tmp_path / "adir"
    directory.mkdir()
    settings.kubernetes_token_file = str(directory)
    assert settings.kubernetes_effective_token == ""


def test_in_cluster_defaults_from_the_pods_service_account(_isolated: Path) -> None:
    (_isolated / "token").write_text("pod-token\n")
    (_isolated / "ca.crt").write_text("CA")
    assert settings.kubernetes_effective_api_url == "https://kubernetes.default.svc"
    assert settings.kubernetes_effective_token == "pod-token"
    assert settings.kubernetes_effective_ca_file == str(_isolated / "ca.crt")


def test_explicit_settings_override_in_cluster_defaults(_isolated: Path) -> None:
    (_isolated / "token").write_text("pod-token")
    (_isolated / "ca.crt").write_text("CA")
    settings.kubernetes_api_url = "https://other:6443"
    settings.kubernetes_token = "mine"
    assert settings.kubernetes_effective_api_url == "https://other:6443"
    assert settings.kubernetes_effective_token == "mine"
    # The pod's cluster CA is never trusted for a different API address.
    assert settings.kubernetes_effective_ca_file == ""


@pytest.mark.asyncio
async def test_in_cluster_import_uses_pod_token_and_ca(client: AsyncClient, headers: dict, _isolated: Path) -> None:
    (_isolated / "token").write_text("pod-token")
    (_isolated / "ca.crt").write_text("CA")
    cfg = (await client.get("/api/v1/kubernetes/config", headers=headers)).json()
    assert cfg["api_url"] == "https://kubernetes.default.svc" and cfg["token_configured"] is True
    with patch("app.api.routes.kubernetes.test_kubernetes_connection", new=AsyncMock(return_value=(True, "ok"))) as t:
        res = await client.post(
            "/api/v1/kubernetes/test-connection", json={"api_url": "https://kubernetes.default.svc"}, headers=headers
        )
    assert res.status_code == 200
    assert t.await_args.kwargs["token"] == "pod-token"
    assert t.await_args.kwargs["ca_file"] == str(_isolated / "ca.crt")


@pytest.mark.asyncio
async def test_pod_token_is_not_sent_to_a_different_api(client: AsyncClient, headers: dict, _isolated: Path) -> None:
    (_isolated / "token").write_text("pod-token")
    res = await client.post(
        "/api/v1/kubernetes/test-connection", json={"api_url": "https://evil.example:6443"}, headers=headers
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_sync_now_reads_the_rotated_token(client: AsyncClient, headers: dict, tmp_path: Path) -> None:
    f = tmp_path / "tok"
    f.write_text("tok-1")
    settings.kubernetes_api_url = "https://k8s.lan:6443"
    settings.kubernetes_token_file = str(f)
    seen: list[str] = []

    async def fake(_run_id, _url, token, *_a):
        seen.append(token)

    with patch("app.api.routes.kubernetes._background_kubernetes_import", new=fake):
        await client.post("/api/v1/kubernetes/sync-now", headers=headers)
        f.write_text("tok-2")
        await client.post("/api/v1/kubernetes/sync-now", headers=headers)
    assert seen == ["tok-1", "tok-2"]
