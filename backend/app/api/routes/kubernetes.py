"""FastAPI router for the Kubernetes import (nodes + workloads) + auto-sync config.

Fetches the cluster's nodes and Deployments/StatefulSets/DaemonSets from the
Kubernetes API and upserts them into the Device Inventory (same review->approve
flow as the Proxmox and Unraid imports).

Credentials: the ServiceAccount token comes from the request body when
provided, else falls back to the server-configured env token - only for the
configured API server, and never with TLS verification off when the server
keeps it on. The token is never persisted by the app and never returned by any
endpoint.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.routes.unraid import _edges_between, _find_existing, _with_row_lists
from app.core.config import settings
from app.core.scheduler import reschedule_kubernetes_sync, set_kubernetes_sync_enabled
from app.db.database import AsyncSessionLocal, get_db
from app.db.models import InventoryDevice, InventoryDeviceLink, Node, ScanRun
from app.schemas.kubernetes import (
    KubernetesConfig,
    KubernetesConnectionRequest,
    KubernetesEdgeOut,
    KubernetesImportPendingResponse,
    KubernetesImportResponse,
    KubernetesNodeOut,
    KubernetesSyncConfig,
    KubernetesTestConnectionResponse,
)
from app.schemas.scan import ScanRunResponse
from app.services.discovery_sources import add_source
from app.services.inventory_sync import attach_device_ids, merge_services
from app.services.kubernetes_service import (
    build_kubernetes_properties,
    fetch_kubernetes_inventory,
    merge_kubernetes_properties,
    normalize_api_url,
    test_kubernetes_connection,
)
from app.services.node_dedupe import dedupe_nodes_by_device

logger = logging.getLogger(__name__)
router = APIRouter()

_K8S_SOURCE = "kubernetes"
_K8S_IEEE_PREFIX = "k8s-"
# Synthetic ieee prefixes a Kubernetes import may take over on a row it matched
# by IP: its own, and UniFi's, which re-finds rows by MAC.
_ADOPTABLE_PREFIXES = (_K8S_IEEE_PREFIX, "unifi-")


def _same_api(a: str, b: str) -> bool:
    return normalize_api_url(a).lower() == normalize_api_url(b).lower()


def _resolve_token(payload: KubernetesConnectionRequest) -> str:
    """The token from the request, else the env token for the configured API only."""
    if payload.token:
        return payload.token

    api_url = settings.kubernetes_effective_api_url
    if not api_url or not _same_api(payload.api_url, api_url):
        raise HTTPException(
            status_code=400,
            detail="Custom Kubernetes API servers require an explicit token",
        )
    # Never send the env token to an endpoint in clear text.
    if normalize_api_url(payload.api_url).lower().startswith("http://"):
        raise HTTPException(
            status_code=400,
            detail="The server-configured Kubernetes token requires an https:// API URL",
        )
    if settings.kubernetes_verify_tls and not payload.verify_tls:
        raise HTTPException(
            status_code=400,
            detail="The server-configured Kubernetes token requires TLS verification",
        )
    token = settings.kubernetes_effective_token
    if not token:
        raise HTTPException(
            status_code=400,
            detail="No Kubernetes token provided and none configured on the server.",
        )
    return token


def _ca_file_for(payload: KubernetesConnectionRequest) -> str | None:
    """The server's CA bundle applies only to the configured API server."""
    api_url = settings.kubernetes_effective_api_url
    if settings.kubernetes_effective_ca_file and api_url and _same_api(payload.api_url, api_url):
        return settings.kubernetes_effective_ca_file
    return None


def _keep(nodes_raw: list[dict[str, Any]], include_offline: bool) -> list[dict[str, Any]]:
    """Drop NotReady nodes and not-ready workloads unless they are wanted."""
    if include_offline:
        return nodes_raw
    return [n for n in nodes_raw if n.get("status") == "online"]


@router.post("/test-connection", response_model=KubernetesTestConnectionResponse)
async def test_connection_endpoint(
    payload: KubernetesConnectionRequest,
    _: str = Depends(get_current_user),
) -> KubernetesTestConnectionResponse:
    """Validate API server reachability + token before importing."""
    token = _resolve_token(payload)
    connected, message = await test_kubernetes_connection(
        api_url=payload.api_url,
        token=token,
        verify_tls=payload.verify_tls,
        ca_file=_ca_file_for(payload),
    )
    return KubernetesTestConnectionResponse(connected=connected, message=message)


@router.post("/import", response_model=KubernetesImportResponse)
async def import_kubernetes(
    payload: KubernetesConnectionRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
) -> KubernetesImportResponse:
    """Fetch nodes + workloads and return them ready for canvas drop.

    Everything returned also lands in the Device Inventory. NotReady nodes and
    not-ready workloads are left out of both unless ``include_offline`` is set.
    """
    token = _resolve_token(payload)
    try:
        nodes_raw, edges_raw, notice = await fetch_kubernetes_inventory(
            api_url=payload.api_url,
            token=token,
            verify_tls=payload.verify_tls,
            ca_file=_ca_file_for(payload),
            namespaces=payload.namespaces,
            include_system=payload.include_system,
        )
    except ConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unexpected error during Kubernetes import")
        raise HTTPException(status_code=500, detail="Unexpected error during Kubernetes import") from exc

    stored = _keep(nodes_raw, payload.include_offline)
    await _persist_pending_import(db, stored, _edges_between(edges_raw, stored))

    drawn = await attach_device_ids(db, stored)
    drawn = await _with_row_lists(db, drawn)
    nodes = [KubernetesNodeOut(**n) for n in drawn]
    edges = [KubernetesEdgeOut(**e) for e in _edges_between(edges_raw, drawn)]
    return KubernetesImportResponse(nodes=nodes, edges=edges, device_count=len(nodes), notice=notice)


@router.post("/import-pending", response_model=ScanRunResponse)
async def import_kubernetes_to_pending(
    payload: KubernetesConnectionRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
) -> ScanRun:
    """Queue a Kubernetes inventory import as a background scan run (kind=kubernetes)."""
    token = _resolve_token(payload)
    run = ScanRun(status="running", kind="kubernetes", ranges=[payload.api_url])
    db.add(run)
    await db.commit()
    await db.refresh(run)
    background_tasks.add_task(
        _background_kubernetes_import,
        run.id,
        payload.api_url,
        token,
        payload.verify_tls,
        _ca_file_for(payload),
        payload.namespaces,
        payload.include_system,
        payload.include_offline,
    )
    return run


@router.post("/sync-now", response_model=ScanRunResponse)
async def sync_kubernetes_now(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
) -> ScanRun:
    """Trigger an immediate Kubernetes sync using the server env config."""
    api_url, token = settings.kubernetes_effective_api_url, settings.kubernetes_effective_token
    if not (api_url and token):
        raise HTTPException(
            status_code=400,
            detail="Cannot sync: no Kubernetes API URL/token configured on the server.",
        )
    run = ScanRun(status="running", kind="kubernetes", ranges=[api_url])
    db.add(run)
    await db.commit()
    await db.refresh(run)
    background_tasks.add_task(
        _background_kubernetes_import,
        run.id,
        api_url,
        token,
        settings.kubernetes_verify_tls,
        settings.kubernetes_effective_ca_file or None,
        settings.kubernetes_namespace_list,
        settings.kubernetes_include_system,
        settings.kubernetes_sync_include_offline,
    )
    return run


async def _background_kubernetes_import(
    run_id: str,
    api_url: str,
    token: str,
    verify_tls: bool,
    ca_file: str | None,
    namespaces: list[str],
    include_system: bool,
    include_offline: bool,
) -> None:
    async with AsyncSessionLocal() as db:
        try:
            nodes_raw, edges_raw, notice = await fetch_kubernetes_inventory(
                api_url=api_url,
                token=token,
                verify_tls=verify_tls,
                ca_file=ca_file,
                namespaces=namespaces,
                include_system=include_system,
            )
            stored = _keep(nodes_raw, include_offline)
            result = await _persist_pending_import(db, stored, _edges_between(edges_raw, stored))
            run = await db.get(ScanRun, run_id)
            if run:
                run.status = "done"
                run.devices_found = result.device_count
                run.finished_at = datetime.now(timezone.utc)
                # Non-fatal advisory: the run is done, the UI shows it as a warning.
                run.error = notice
                await db.commit()
            from app.api.routes.status import broadcast_scan_update
            await broadcast_scan_update(run_id=run_id, devices_found=result.device_count)
        except Exception as exc:
            logger.exception("Kubernetes import %s failed", run_id)
            await db.rollback()
            run = await db.get(ScanRun, run_id)
            if run:
                run.status = "error"
                run.error = str(exc)[:500]
                run.finished_at = datetime.now(timezone.utc)
                await db.commit()


async def _persist_pending_import(
    db: AsyncSession,
    nodes_raw: list[dict[str, Any]],
    edges_raw: list[dict[str, Any]],
) -> KubernetesImportPendingResponse:
    """Upsert nodes and workloads into device_inventory, replace the cluster's links.

    Update-in-place only: nothing is deleted, hidden rows stay hidden, and a row
    approved earlier but drawn on no canvas any more is revived to pending.
    """
    await dedupe_nodes_by_device(db)

    pending_created = 0
    pending_updated = 0
    for n in nodes_raw:
        ieee = n.get("ieee_address")
        if not ieee:
            continue
        props = build_kubernetes_properties(n)
        row = await _find_existing(db, ieee, n.get("ip"), n.get("mac"))
        if row is None:
            db.add(_new_row(ieee, n, props))
            pending_created += 1
            continue
        drawn = (
            await db.execute(select(Node.id).where(Node.device_id == row.id).limit(1))
        ).scalar_one_or_none() is not None
        _refresh_row(row, ieee, n, props)
        if row.status == "approved" and not drawn:
            row.status = "pending"
        pending_updated += 1

    links_recorded = await _replace_links(db, nodes_raw, edges_raw)
    await db.commit()
    return KubernetesImportPendingResponse(
        pending_created=pending_created,
        pending_updated=pending_updated,
        links_recorded=links_recorded,
        device_count=len(nodes_raw),
    )


def _new_row(ieee: str, n: dict[str, Any], props: list[dict[str, Any]]) -> InventoryDevice:
    return InventoryDevice(
        ieee_address=ieee,
        ip=n.get("ip"),
        mac=n.get("mac"),
        hostname=n.get("hostname"),
        friendly_name=n.get("label"),
        suggested_type=n.get("type"),
        vendor=n.get("vendor"),
        model=n.get("model"),
        os=n.get("os_version"),
        cpu_count=n.get("cpu_count"),
        ram_gb=n.get("ram_gb"),
        services=list(n.get("services") or []),
        properties=props,
        status="pending",
        discovery_source=_K8S_SOURCE,
        discovery_sources=[_K8S_SOURCE],
    )


def _refresh_row(row: InventoryDevice, ieee: str, n: dict[str, Any], props: list[dict[str, Any]]) -> None:
    row.discovery_sources = add_source(
        add_source(row.discovery_sources, row.discovery_source), _K8S_SOURCE
    )
    if not row.ieee_address or row.ieee_address.startswith(_ADOPTABLE_PREFIXES):
        row.ieee_address = ieee
    row.ip = n.get("ip") or row.ip
    row.mac = row.mac or n.get("mac")
    row.hostname = n.get("hostname") or row.hostname
    row.friendly_name = n.get("label") or row.friendly_name
    row.suggested_type = n.get("type") or row.suggested_type
    row.vendor = n.get("vendor") or row.vendor
    row.model = n.get("model") or row.model
    row.os = n.get("os_version") or row.os
    row.cpu_count = row.cpu_count or n.get("cpu_count")
    row.ram_gb = row.ram_gb or n.get("ram_gb")
    row.properties = merge_kubernetes_properties(list(row.properties or []), props)
    # discovered=True: refresh the address, keep a name or icon the user set.
    row.services = merge_services(row.services, n.get("services"), discovered=True)


async def _replace_links(
    db: AsyncSession,
    nodes_raw: list[dict[str, Any]],
    edges_raw: list[dict[str, Any]],
) -> int:
    """Replace the imported cluster nodes' node->workload links with the fresh set.

    Scoped to the imported nodes so a second cluster keeps its own links.
    """
    hosts = [n["ieee_address"] for n in nodes_raw if n.get("type") == "server"]
    if hosts:
        await db.execute(
            sa_delete(InventoryDeviceLink).where(
                InventoryDeviceLink.discovery_source == _K8S_SOURCE,
                InventoryDeviceLink.source_ieee.in_(hosts),
            )
        )
    recorded = 0
    seen: set[tuple[str, str]] = set()
    for e in edges_raw:
        src, tgt = e.get("source"), e.get("target")
        if not src or not tgt or (src, tgt) in seen:
            continue
        seen.add((src, tgt))
        db.add(InventoryDeviceLink(source_ieee=src, target_ieee=tgt, discovery_source=_K8S_SOURCE))
        recorded += 1
    return recorded


@router.get("/config", response_model=KubernetesConfig)
async def get_kubernetes_config(_: str = Depends(get_current_user)) -> KubernetesConfig:
    """Return non-secret Kubernetes config. Never includes the token."""
    return KubernetesConfig(
        api_url=settings.kubernetes_effective_api_url,
        verify_tls=settings.kubernetes_verify_tls,
        namespaces=settings.kubernetes_namespace_list,
        include_system=settings.kubernetes_include_system,
        sync_enabled=settings.kubernetes_sync_enabled,
        sync_interval=settings.kubernetes_sync_interval,
        include_offline=settings.kubernetes_sync_include_offline,
        token_configured=bool(settings.kubernetes_effective_token),
    )


@router.post("/config", response_model=KubernetesConfig)
async def save_kubernetes_config(
    payload: KubernetesSyncConfig,
    _: str = Depends(get_current_user),
) -> KubernetesConfig:
    """Persist the auto-sync activation and apply it live.

    Connection settings (API URL, token or token file, CA, TLS, namespaces) are env-only and
    are never accepted or persisted here.
    """
    if payload.sync_enabled and not (settings.kubernetes_effective_api_url and settings.kubernetes_effective_token):
        raise HTTPException(
            status_code=400,
            detail="Cannot enable auto-sync: no Kubernetes API URL/token configured in the server env.",
        )
    try:
        settings.kubernetes_sync_enabled = payload.sync_enabled
        settings.kubernetes_sync_interval = payload.sync_interval
        settings.kubernetes_sync_include_offline = payload.include_offline
        settings.save_overrides()
        set_kubernetes_sync_enabled(payload.sync_enabled)
        if payload.sync_enabled:
            reschedule_kubernetes_sync(payload.sync_interval)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return await get_kubernetes_config()
