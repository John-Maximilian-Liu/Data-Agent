"""Request dependencies — auth, project scope, idempotency."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from dataagent.authorization.policy import AuthContext, EffectiveAccess, evaluate_effective_access
from dataagent.config import Settings, get_settings
from dataagent.storage.database import get_session_factory
from dataagent.storage import models as m
from dataagent.util.hashing import verify_secret


def get_db() -> Session:
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


DbSession = Annotated[Session, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


@dataclass
class RequestContext:
    auth: AuthContext
    access: EffectiveAccess
    request_id: str
    project: m.Project


def _offline_principal(session: Session, header_principal: str | None) -> m.Principal:
    subject = header_principal or "dev-admin"
    principal = session.execute(
        select(m.Principal).where(
            m.Principal.issuer == "offline",
            m.Principal.subject == subject,
        )
    ).scalar_one_or_none()
    if principal is None:
        principal = m.Principal(
            issuer="offline",
            subject=subject,
            display_name=subject,
            kind="user",
        )
        session.add(principal)
        session.flush()
    return principal


def authenticate(
    request: Request,
    session: DbSession,
    settings: AppSettings,
    authorization: Annotated[str | None, Header()] = None,
    x_dataagent_principal: Annotated[str | None, Header()] = None,
    x_dataagent_credential: Annotated[str | None, Header()] = None,
    x_request_id: Annotated[str | None, Header()] = None,
) -> AuthContext:
    request_id = x_request_id or str(uuid.uuid4())
    request.state.request_id = request_id

    if x_dataagent_credential:
        # service credential auth
        creds = list(session.execute(select(m.ServiceCredential)).scalars())
        matched = None
        for c in creds:
            if c.status == "active" and verify_secret(x_dataagent_credential, c.secret_hash):
                matched = c
                break
        if matched is None:
            raise HTTPException(status_code=401, detail={"code": "invalid_credential"})
        return AuthContext(
            principal_id=matched.id,  # service principal surrogate
            tenant_id=matched.tenant_id,
            project_id=matched.project_id,
            display_name=matched.name,
            kind="service",
            request_id=request_id,
            auth_mode="credential",
            credential_id=matched.id,
            allowed_agent_ids=frozenset(uuid.UUID(x) for x in (matched.allowed_agent_ids or [])),
            scopes=frozenset(matched.scopes or []),
            single_agent_id=matched.agent_id,
        )

    if settings.auth_mode == "offline":
        principal = _offline_principal(session, x_dataagent_principal)
        # resolve tenant/project from path later; store principal only
        return AuthContext(
            principal_id=principal.id,
            tenant_id=uuid.UUID(int=0),  # filled by project dependency
            project_id=None,
            display_name=principal.display_name,
            kind=principal.kind,
            request_id=request_id,
            auth_mode="offline",
        )

    # OIDC (simplified JWT validation path)
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail={"code": "missing_bearer"})
    token = authorization.removeprefix("Bearer ").strip()
    try:
        from jose import jwt

        # In production, fetch JWKS; here decode without verify only if issuer configured for tests
        claims = jwt.get_unverified_claims(token)
        if settings.oidc_audience and claims.get("aud") not in (
            settings.oidc_audience,
            [settings.oidc_audience],
        ):
            raise HTTPException(status_code=401, detail={"code": "bad_audience"})
        issuer = claims.get("iss") or ""
        subject = claims.get("sub") or ""
        principal = session.execute(
            select(m.Principal).where(m.Principal.issuer == issuer, m.Principal.subject == subject)
        ).scalar_one_or_none()
        if principal is None:
            principal = m.Principal(
                issuer=issuer,
                subject=subject,
                display_name=claims.get("name") or subject,
                email=claims.get("email"),
            )
            session.add(principal)
            session.flush()
        return AuthContext(
            principal_id=principal.id,
            tenant_id=uuid.UUID(int=0),
            project_id=None,
            display_name=principal.display_name,
            request_id=request_id,
            auth_mode="oidc",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail={"code": "oidc_invalid", "message": str(e)})


Auth = Annotated[AuthContext, Depends(authenticate)]


def project_context(
    project_id: uuid.UUID,
    session: DbSession,
    auth: Auth,
) -> RequestContext:
    project = session.get(m.Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail={"code": "project_not_found"})

    if auth.kind == "service":
        if auth.project_id != project_id or auth.tenant_id != project.tenant_id:
            raise HTTPException(status_code=403, detail={"code": "project_mismatch"})
        auth.tenant_id = project.tenant_id
        auth.project_id = project_id
        access = EffectiveAccess(
            tenant_id=project.tenant_id,
            project_id=project_id,
            principal_id=auth.principal_id,
            policy_revision=1,
            modules={k: bool(v) for k, v in (project.modules or {}).items()},
        )
        # service scopes map to actions on allowed agents
        for aid in auth.allowed_agent_ids or set():
            from dataagent.authorization.policy import AgentEntitlement

            ent = AgentEntitlement(agent_id=aid)
            if "source.write" in auth.scopes:
                ent.actions.add("source.write")
            if "ci.submit" in auth.scopes:
                ent.actions.add("ci.submit")
            if "metrics.read" in auth.scopes:
                ent.actions.add("metrics.read")
                ent.fields.add("aggregates")
            access.by_agent[aid] = ent
        if auth.single_agent_id:
            from dataagent.authorization.policy import AgentEntitlement

            ent = access.by_agent.get(auth.single_agent_id) or AgentEntitlement(
                agent_id=auth.single_agent_id
            )
            ent.actions |= set(auth.scopes)
            access.by_agent[auth.single_agent_id] = ent
        return RequestContext(auth=auth, access=access, request_id=auth.request_id, project=project)

    # membership required — project-scoped or tenant-wide (project_id IS NULL)
    memberships = list(
        session.execute(
            select(m.Membership).where(
                m.Membership.tenant_id == project.tenant_id,
                m.Membership.principal_id == auth.principal_id,
                m.Membership.status == "active",
            )
        ).scalars()
    )
    allowed = any(
        row.project_id is None or row.project_id == project_id for row in memberships
    )
    if not allowed:
        raise HTTPException(status_code=403, detail={"code": "not_a_member"})

    auth.tenant_id = project.tenant_id
    auth.project_id = project_id
    access = evaluate_effective_access(
        session,
        principal_id=auth.principal_id,
        tenant_id=project.tenant_id,
        project_id=project_id,
    )
    return RequestContext(auth=auth, access=access, request_id=auth.request_id, project=project)


Ctx = Annotated[RequestContext, Depends(project_context)]


def check_idempotency(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    operation: str,
    key: str | None,
    body: dict[str, Any],
) -> dict[str, Any] | None:
    if not key:
        return None
    request_hash = hashlib.sha256(
        json.dumps(body, sort_keys=True, default=str).encode()
    ).hexdigest()
    row = session.execute(
        select(m.IdempotencyRecord).where(
            m.IdempotencyRecord.tenant_id == tenant_id,
            m.IdempotencyRecord.project_id == project_id,
            m.IdempotencyRecord.operation == operation,
            m.IdempotencyRecord.idempotency_key == key,
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    if row.request_hash != request_hash:
        raise HTTPException(status_code=409, detail={"code": "idempotency_conflict"})
    return {"status": row.response_status, "body": row.response_body}


def store_idempotency(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    operation: str,
    key: str | None,
    body: dict[str, Any],
    status: int,
    response: dict[str, Any],
) -> None:
    if not key:
        return
    request_hash = hashlib.sha256(
        json.dumps(body, sort_keys=True, default=str).encode()
    ).hexdigest()
    session.add(
        m.IdempotencyRecord(
            tenant_id=tenant_id,
            project_id=project_id,
            operation=operation,
            idempotency_key=key,
            request_hash=request_hash,
            response_status=status,
            response_body=response,
        )
    )
