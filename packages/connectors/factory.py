from __future__ import annotations

from decimal import Decimal
from typing import Any

from packages.compute.inventory import ComputeNode, Gpu, MigSlice, NodeState
from packages.connectors.compute.base import ComputeAdapter
from packages.connectors.kubernetes import KubernetesAdapter
from packages.connectors.meters import HttpsMeterConnector
from packages.connectors.simulator import SimulatorComputeAdapter
from packages.connectors.slurm import SlurmAdapter
from packages.secrets import CredentialResolver


def _gpu(document: dict[str, Any]) -> Gpu:
    return Gpu(
        id=str(document["id"]),
        model=str(document["model"]),
        memory_gb=Decimal(str(document["memory_gb"])),
        max_power_kw=Decimal(str(document["max_power_kw"])),
        mig_slices=tuple(
            MigSlice(str(item["id"]), Decimal(str(item["memory_gb"])), Decimal(str(item["compute_fraction"])))
            for item in document.get("mig_slices", [])
        ),
    )


def _nodes(documents: object, tenant_id: str) -> tuple[ComputeNode, ...]:
    if not isinstance(documents, list) or not documents:
        raise ValueError("simulator connector requires at least one node")
    nodes = tuple(
        ComputeNode(
            id=str(item["id"]),
            tenant_id=tenant_id,
            site_id=str(item["site_id"]),
            topology_asset_id=str(item["topology_asset_id"]),
            gpus=tuple(_gpu(gpu) for gpu in item.get("gpus", [])),
            cpu_cores=int(item.get("cpu_cores", 0)),
            memory_gb=Decimal(str(item.get("memory_gb", 0))),
            state=NodeState(str(item.get("state", "ready"))),
            labels={str(key): str(value) for key, value in dict(item.get("labels", {})).items()},
        )
        for item in documents
        if isinstance(item, dict)
    )
    if len(nodes) != len(documents):
        raise ValueError("simulator connector node document is invalid")
    return nodes


def create_compute_adapter(
    connector_id: str,
    configuration: dict[str, Any],
    *,
    tenant_id: str,
    resolver: CredentialResolver | None = None,
) -> ComputeAdapter:
    resolver = resolver or CredentialResolver.from_env()
    connector_type = str(configuration.get("type", ""))
    if connector_type == "simulator":
        return SimulatorComputeAdapter(connector_id, _nodes(configuration.get("nodes"), tenant_id))
    endpoint = str(configuration.get("endpoint", ""))
    if not endpoint.startswith("https://"):
        raise ValueError("external compute connector endpoint must use HTTPS")
    credential_ref = configuration.get("credential_ref")
    service_account = connector_type == "kubernetes" and configuration.get("auth_mode") == "service_account"
    if not service_account and not isinstance(credential_ref, str):
        raise ValueError("external compute connector requires credential_ref")
    credential = resolver.resolve(credential_ref) if isinstance(credential_ref, str) else None
    site_id = str(configuration.get("site_id", ""))
    if not site_id:
        raise ValueError("external compute connector requires site_id")
    ca_bundle: str | bool = True
    if isinstance(configuration.get("ca_bundle_ref"), str):
        ca_bundle = str(resolver.resolve_file(str(configuration["ca_bundle_ref"])))
    if connector_type == "kubernetes":
        return KubernetesAdapter(
            connector_id,
            api_url=endpoint,
            token=credential,
            ca_bundle=ca_bundle,
            tenant_id=tenant_id,
            site_id=site_id,
        )
    if connector_type == "slurm":
        return SlurmAdapter(
            connector_id,
            api_url=endpoint,
            jwt_token=credential,
            user_name=str(configuration["user_name"]) if configuration.get("user_name") else None,
            ca_bundle=ca_bundle,
            tenant_id=tenant_id,
            site_id=site_id,
        )
    raise ValueError(f"unsupported compute connector type {connector_type!r}")


def create_meter_connector(
    connector_id: str,
    configuration: dict[str, Any],
    *,
    resolver: CredentialResolver | None = None,
) -> HttpsMeterConnector:
    if configuration.get("type") != "https_meter":
        raise ValueError("connector is not an HTTPS meter")
    resolver = resolver or CredentialResolver.from_env()
    endpoint = str(configuration.get("endpoint", ""))
    credential_ref = configuration.get("credential_ref")
    token = resolver.resolve(credential_ref) if isinstance(credential_ref, str) else None
    ca_bundle: str | bool = True
    if isinstance(configuration.get("ca_bundle_ref"), str):
        ca_bundle = str(resolver.resolve_file(str(configuration["ca_bundle_ref"])))
    certificate_ref = configuration.get("client_certificate_ref")
    key_ref = configuration.get("client_key_ref")
    if bool(certificate_ref) != bool(key_ref):
        raise ValueError("meter mTLS certificate and key references must be configured together")
    certificate = (
        (str(resolver.resolve_file(str(certificate_ref))), str(resolver.resolve_file(str(key_ref))))
        if certificate_ref and key_ref
        else None
    )
    if token is None and certificate is None:
        raise ValueError("meter connector requires a token or mTLS credential references")
    return HttpsMeterConnector(
        connector_id=connector_id,
        base_url=endpoint,
        token=token,
        ca_bundle=ca_bundle,
        client_certificate=certificate,
    )
