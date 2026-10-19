"""Pydantic v2 schemas for the Kubernetes import.

The ServiceAccount token is accepted on requests only and is optional - when
omitted the backend falls back to the server-configured token (env), for the
configured API server only. No response schema ever carries the token.
"""

from typing import Any

from pydantic import BaseModel, Field


class KubernetesConnectionRequest(BaseModel):
    api_url: str = Field(..., description="Kubernetes API server URL, e.g. https://192.168.1.10:6443")
    token: str | None = Field(None, description="ServiceAccount bearer token (falls back to server env)")
    verify_tls: bool = Field(True, description="Verify the API server's TLS certificate")
    # Empty = every namespace except the system ones (unless include_system).
    namespaces: list[str] = Field(default_factory=list, description="Only import these namespaces")
    include_system: bool = Field(False, description="Also import kube-system, kube-public, kube-node-lease")
    # Scaled-to-zero / not-ready workloads and NotReady nodes: imported like
    # healthy ones, or left out.
    include_offline: bool = True


class KubernetesTestConnectionResponse(BaseModel):
    connected: bool
    message: str


class KubernetesNodeOut(BaseModel):
    """A homelable-ready node for a cluster node or one of its workloads."""

    id: str
    label: str
    type: str  # server | docker_container
    ieee_address: str
    hostname: str | None = None
    ip: str | None = None
    status: str
    vendor: str | None = None
    model: str | None = None
    parent_ieee: str | None = None
    # The Device Inventory row this node draws, stamped by the import.
    device_id: str | None = None
    # The row's lists, so the node's first canvas save writes them back intact.
    services: list[dict[str, Any]] = []
    properties: list[dict[str, Any]] = []


class KubernetesEdgeOut(BaseModel):
    source: str
    target: str


class KubernetesImportResponse(BaseModel):
    nodes: list[KubernetesNodeOut]
    edges: list[KubernetesEdgeOut]
    device_count: int
    # Why part of the cluster was left out (e.g. the token cannot list pods).
    notice: str | None = None


class KubernetesImportPendingResponse(BaseModel):
    pending_created: int
    pending_updated: int
    links_recorded: int
    device_count: int


class KubernetesConfig(BaseModel):
    """Non-secret Kubernetes connection + auto-sync config (GET response)."""

    api_url: str = ""
    verify_tls: bool = True
    namespaces: list[str] = []
    include_system: bool = False
    sync_enabled: bool = False
    sync_interval: int = Field(3600, ge=300)
    include_offline: bool = True
    token_configured: bool = False


class KubernetesSyncConfig(BaseModel):
    """User-editable auto-sync config (POST body). Connection fields are env-only."""

    sync_enabled: bool = False
    sync_interval: int = Field(3600, ge=300, le=86400)
    include_offline: bool = True
