"""Stable tenant and principal identity mappings.

Only authentication subjects and one-way API-key fingerprints cross into the
control plane. Raw credentials are never retained in these values.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID, uuid5


IDENTITY_NAMESPACE = UUID("497ec8de-f186-488a-87a4-826e5fd1de19")


class TenantKind(StrEnum):
    PERSONAL = "personal"
    ORGANIZATION = "organization"
    SERVICE = "service"
    LOCAL_DEVELOPMENT = "local_development"
    QUARANTINE = "quarantine"


class PrincipalKind(StrEnum):
    USER = "user"
    SERVICE = "service"
    LOCAL_ANONYMOUS = "local_anonymous"
    QUARANTINE = "quarantine"


@dataclass(frozen=True, slots=True)
class PrincipalContext:
    tenant_id: UUID
    principal_id: UUID
    tenant_kind: TenantKind
    kind: PrincipalKind
    external_subject: str
    api_key_fingerprint: str | None = field(default=None, repr=False)
    quarantined: bool = False


def _stable_uuid(value: str) -> UUID:
    return uuid5(IDENTITY_NAMESPACE, value)


def user_principal(
    user_id: str,
    *,
    tenant_id: UUID | None = None,
    tenant_kind: TenantKind = TenantKind.PERSONAL,
) -> PrincipalContext:
    subject = user_id.strip()
    if not subject:
        raise ValueError("user_id must not be empty")
    resolved_tenant_id = tenant_id or _stable_uuid(f"personal-tenant:{subject}")
    return PrincipalContext(
        tenant_id=resolved_tenant_id,
        principal_id=_stable_uuid(
            f"user-principal:{resolved_tenant_id}:{subject}"
        ),
        tenant_kind=tenant_kind,
        kind=PrincipalKind.USER,
        external_subject=f"user:{subject}",
    )


def api_key_fingerprint(api_key: str) -> str:
    if not api_key:
        raise ValueError("api_key must not be empty")
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def api_key_principal(api_key: str) -> PrincipalContext:
    fingerprint = api_key_fingerprint(api_key)
    return PrincipalContext(
        tenant_id=_stable_uuid(f"service-tenant:{fingerprint}"),
        principal_id=_stable_uuid(f"service-principal:{fingerprint}"),
        tenant_kind=TenantKind.SERVICE,
        kind=PrincipalKind.SERVICE,
        external_subject=f"api-key-sha256:{fingerprint}",
        api_key_fingerprint=fingerprint,
    )


def platform_service_principal(service_name: str) -> PrincipalContext:
    """Return a stable first-party service identity without a reusable secret."""
    name = service_name.strip().lower()
    if not name:
        raise ValueError("service_name must not be empty")
    tenant_id = _stable_uuid(f"platform-service-tenant:{name}")
    return PrincipalContext(
        tenant_id=tenant_id,
        principal_id=_stable_uuid(f"platform-service-principal:{name}"),
        tenant_kind=TenantKind.SERVICE,
        kind=PrincipalKind.SERVICE,
        external_subject=f"platform-service:{name}",
        api_key_fingerprint=hashlib.sha256(
            f"platform-service:{name}".encode("utf-8")
        ).hexdigest(),
    )


def local_anonymous_principal() -> PrincipalContext:
    return PrincipalContext(
        tenant_id=_stable_uuid("local-development-tenant"),
        principal_id=_stable_uuid("local-development-anonymous-principal"),
        tenant_kind=TenantKind.LOCAL_DEVELOPMENT,
        kind=PrincipalKind.LOCAL_ANONYMOUS,
        external_subject="local-development:anonymous",
    )


def quarantine_principal(source_reference: str) -> PrincipalContext:
    reference = source_reference.strip() or "unknown-owner"
    return PrincipalContext(
        tenant_id=_stable_uuid("quarantine-tenant"),
        principal_id=_stable_uuid(f"quarantine-principal:{reference}"),
        tenant_kind=TenantKind.QUARANTINE,
        kind=PrincipalKind.QUARANTINE,
        external_subject=f"quarantine:{reference}",
        quarantined=True,
    )
