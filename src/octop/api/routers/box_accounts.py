"""HTTP adapters for local box ownership, device pairing and short sessions."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from octop.api.common.box_auth import (
    BOX_CSRF_HEADER,
    browser_cookie_name,
    require_box_route,
    require_box_transport,
    require_browser_origin,
)
from octop.api.common.client_ip import resolve_client_ip
from octop.api.deps import get_server
from octop.api.openapi_meta import box_auth_metadata
from octop.infra.auth.box_accounts import BoxAccountService, BoxAuthContext
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.source_routes import local_instance
from octop.infra.server import OctopServer


async def _service(
    request: Request, server: OctopServer = Depends(get_server)
) -> BoxAccountService:
    service = server.box_account_service
    if service is None:
        raise OctopError.localized(
            ErrorCode.NOT_FOUND,
            details={"reason": "box_mode_required"},
        )
    require_box_transport(
        request.scope, http_origins=server.config.box_http_origins if server.config else ()
    )
    return service


async def _public_service(
    request: Request, service: BoxAccountService = Depends(_service)
) -> BoxAccountService:
    await asyncio.to_thread(service.rate_limit, resolve_client_ip(request))
    return service


async def _context(
    request: Request,
    service: BoxAccountService = Depends(_service),
) -> BoxAuthContext:
    context = getattr(request.state, "box_auth_context", None)
    if not isinstance(context, BoxAuthContext):
        raise OctopError.localized(ErrorCode.AUTH_FAILED)
    validated = await asyncio.to_thread(service.validate_context, context)
    require_box_route(validated.role, request.method, request.url.path)
    return validated


router = APIRouter(prefix="/box")
auth_router = APIRouter(prefix="/auth")


class BoxRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PublicKeyJwk(BoxRequest):
    kty: Literal["EC"]
    crv: Literal["P-256"]
    x: str = Field(min_length=43, max_length=43)
    y: str = Field(min_length=43, max_length=43)


class BoxStatusResponse(BaseModel):
    account_mode: Literal["box_local"]
    protocol_version: Literal[1]
    auth_model_version: Literal[2]
    multiple_owner_devices: Literal[True] = Field(
        description="Owner access can be granted to multiple independently revocable clients."
    )
    management_key_configured: bool
    primary_client_id: str | None = Field(
        deprecated=True,
        description="Legacy representative owner device; never an authorization selector.",
    )
    box_id: str
    state: Literal["unclaimed", "claimed", "recovery_required"]
    pairing_available: bool


class ChallengeResponse(BaseModel):
    challenge_id: str
    payload: dict[str, JsonValue] = Field(
        description="Sign this exact JSON payload with ES256; do not add or change claims."
    )
    expires_at: int


class PairingRequest(BoxRequest):
    grant_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="Optional grant identifier; when omitted, the one-time secret identifies the grant.",
    )
    secret: str = Field(
        min_length=1,
        max_length=512,
        description="One-time secret from a trusted local grant; never place in a URL.",
    )
    request_id: str = Field(
        min_length=1,
        max_length=128,
        description="Stable identifier for retrying this candidate registration.",
    )
    public_key_jwk: PublicKeyJwk
    name: str = Field(min_length=1, max_length=120)
    platform: str = Field(min_length=1, max_length=40)


class PrimaryClaimRequest(BoxRequest):
    management_key: str = Field(min_length=1, max_length=512)
    request_id: str = Field(min_length=1, max_length=128)
    public_key_jwk: PublicKeyJwk
    name: str = Field(min_length=1, max_length=120)
    platform: str = Field(min_length=1, max_length=40)


class AdminLoginRequest(BoxRequest):
    management_key: str = Field(min_length=1, max_length=512)


class RotateKeyRequest(BoxRequest):
    revoke_clients: bool = False


class RotateKeyResponse(BaseModel):
    management_key: str
    auth_revision: int


class ProofRequest(BoxRequest):
    proof: str = Field(
        min_length=1,
        max_length=16384,
        description="Compact ES256 JWS; protected header is alg=ES256, typ=octop-box-proof+jwt.",
    )


class PairingResponse(BaseModel):
    pairing_id: str
    state: str
    public_key_fingerprint: str
    client_id: str | None = None
    version: Literal[1]
    challenge: ChallengeResponse | None = None


class ChallengeRequest(BoxRequest):
    client_id: str | None = Field(default=None, max_length=128)
    pairing_id: str | None = Field(default=None, max_length=128)
    action: Literal["session", "enroll", "result"] = "session"
    params: dict[str, str] = Field(
        default_factory=dict,
        max_length=4,
        description="Action parameters bound into the signature; exact keys depend on action.",
    )


class SessionResponse(BaseModel):
    session_kind: Literal["client"]
    role: Literal["primary", "guest"]
    user_id: int
    access_token: str
    token_type: Literal["bearer"]
    expires_in: int
    session_id: str
    paired_client_id: str


class BrowserSessionResponse(BaseModel):
    session_id: str
    paired_client_id: None = None
    session_kind: Literal["admin"]
    role: Literal["admin"]
    expires_in: int
    csrf_token: str = Field(
        description="Send as X-Octop-Box-CSRF on cookie-authenticated mutations."
    )


class GrantRequest(BoxRequest):
    request_id: str = Field(min_length=1, max_length=128)
    proof: str | None = Field(default=None, min_length=1, max_length=16384)


class GrantResponse(BaseModel):
    grant_id: str
    purpose: Literal["claim", "enroll"]
    secret: str
    expires_at: int = Field(
        description="Expiry as Unix epoch seconds. New guest shares expire 48 hours after issuance."
    )


class OwnerResponse(BaseModel):
    id: int
    username: str
    display_name: str | None


class BoxAccountResponse(BaseModel):
    box_id: str
    user: OwnerResponse
    owner: OwnerResponse | None = None
    role: Literal["admin", "primary", "guest"]
    session_kind: Literal["admin", "client"]
    primary_client_id: str | None = Field(
        deprecated=True,
        description="Legacy representative owner device; never an authorization selector.",
    )
    owner_client_ids: list[str] = Field(
        description="Active owner devices; empty for guest callers. Wire role primary means owner access."
    )
    auth_revision: int
    recovery_key_confirmed: bool
    instance_id: str
    account_id: str


class PairedClientResponse(BaseModel):
    client_id: str
    role: Literal["primary", "guest", "legacy"]
    name: str
    platform: str
    public_key_fingerprint: str
    state: Literal["active", "revoked"]
    created_at: int
    last_seen_at: int | None
    revoked_at: int | None


Service = Annotated[BoxAccountService, Depends(_service)]
PublicService = Annotated[BoxAccountService, Depends(_public_service)]
Context = Annotated[BoxAuthContext, Depends(_context)]


@router.get(
    "/status",
    openapi_extra={"security": []},
    response_model=BoxStatusResponse,
    summary="Read local box claim status",
)
async def status(service: Service) -> BoxStatusResponse:
    return BoxStatusResponse.model_validate(await asyncio.to_thread(service.status))


@router.post(
    "/pairings",
    openapi_extra={"security": []},
    response_model=PairingResponse,
    summary="Bind a pairing grant to a candidate public key",
)
async def create_pairing(body: PairingRequest, service: PublicService) -> PairingResponse:
    result = await asyncio.to_thread(service.create_pairing, **body.model_dump())
    return PairingResponse.model_validate(result)


@router.post(
    "/primary-claims",
    openapi_extra={"security": []},
    response_model=PairingResponse,
    summary="Register an owner device (legacy path)",
    deprecated=True,
)
@router.post(
    "/owner-claims",
    openapi_extra={"security": []},
    response_model=PairingResponse,
    summary="Register another owner device using the management key",
    description="Proof of the candidate private key is required. Existing owner devices keep access. The primary wire role denotes shared owner access.",
)
async def create_primary_claim(
    body: PrimaryClaimRequest, service: PublicService
) -> PairingResponse:
    return PairingResponse.model_validate(
        await asyncio.to_thread(service.create_primary_claim, **body.model_dump())
    )


async def _register_owner(server: OctopServer, service: BoxAccountService) -> None:
    owner = await asyncio.to_thread(service.owner_user)
    if owner is not None:
        assert server.user_manager is not None
        server.user_manager.register_cached_user(owner)
        await server.user_manager.ensure_resident(owner.id)


async def _register_client(
    server: OctopServer, service: BoxAccountService, client_id: str | None
) -> None:
    if client_id is None:
        return
    user = await asyncio.to_thread(service.user_for_client, client_id)
    if user is not None:
        assert server.user_manager is not None
        server.user_manager.register_cached_user(user)
        if user.is_admin:
            await server.user_manager.ensure_resident(user.id)


@router.post(
    "/pairings/{pairing_id}/proof",
    openapi_extra={"security": []},
    response_model=PairingResponse,
    summary="Prove possession of the candidate private key",
    description="Completes an explicitly authorized primary claim or consumes a one-use share to register an independent guest. No approval step or access token is returned.",
)
async def prove_pairing(
    pairing_id: str,
    body: ProofRequest,
    service: PublicService,
    server: OctopServer = Depends(get_server),
) -> PairingResponse:
    result = await asyncio.to_thread(service.prove_pairing, pairing_id, body.proof)
    await _register_client(server, service, result.get("client_id"))
    return PairingResponse.model_validate(result)


@router.post(
    "/pairings/{pairing_id}/result",
    openapi_extra={"security": []},
    response_model=PairingResponse,
    summary="Read a pairing result with a fresh candidate signature",
)
async def pairing_result(
    pairing_id: str,
    body: ProofRequest,
    service: PublicService,
    server: OctopServer = Depends(get_server),
) -> PairingResponse:
    result = await asyncio.to_thread(service.pairing_result, pairing_id, body.proof)
    await _register_client(server, service, result.get("client_id"))
    return PairingResponse.model_validate(result)


@auth_router.post(
    "/challenges",
    openapi_extra={"security": []},
    response_model=ChallengeResponse,
    summary="Request a one-time client signature challenge",
    description="Session and sensitive-action challenges bind a registered device key. Result challenges use the candidate key bound to the pairing. Valid for 60 seconds; consume once.",
)
async def challenge(body: ChallengeRequest, service: PublicService) -> ChallengeResponse:
    return ChallengeResponse.model_validate(
        await asyncio.to_thread(service.create_challenge, **body.model_dump())
    )


@auth_router.post(
    "/sessions",
    openapi_extra={"security": []},
    response_model=SessionResponse,
    summary="Exchange a client signature for a short access session",
    description="Returns a 10-minute revocable Bearer JWT. There is no refresh token or sliding renewal; obtain a new signature challenge to reconnect.",
)
async def create_session(
    body: ProofRequest, response: Response, service: PublicService
) -> SessionResponse:
    response.headers["Cache-Control"] = "no-store"
    return SessionResponse.model_validate(
        await asyncio.to_thread(service.create_session, body.proof)
    )


@auth_router.post(
    "/browser/sessions",
    openapi_extra={
        "security": [],
        "parameters": [
            {
                "name": "Origin",
                "in": "header",
                "required": True,
                "schema": {"type": "string"},
                "description": "Exact same-origin browser origin.",
            },
            {
                "name": BOX_CSRF_HEADER,
                "in": "header",
                "required": False,
                "schema": {"type": "string"},
                "description": "Required when replacing an existing valid admin cookie; omitted for initial management-key login.",
            },
        ],
    },
    response_model=BrowserSessionResponse,
    summary="Sign into Web administration using the management key",
    description="Requires same-origin Origin and, for an existing active cookie, its CSRF token. Creates a 24-hour HttpOnly admin session without registering a device. Native signatures are not accepted.",
)
async def create_browser_session(
    body: AdminLoginRequest,
    request: Request,
    response: Response,
    service: PublicService,
    server: OctopServer = Depends(get_server),
) -> BrowserSessionResponse:
    require_browser_origin(
        request.scope, http_origins=server.config.box_http_origins if server.config else ()
    )
    previous = None
    raw = request.cookies.get(browser_cookie_name(service.box_id))
    if raw:
        try:
            previous = await asyncio.to_thread(service.validate_access_token, raw)
        except OctopError as exc:
            # Expired or revoked cookies must not prevent key-based reconnection.
            if exc.status != 401:
                raise
        if previous is not None:
            await asyncio.to_thread(
                service.require_browser_csrf, previous, request.headers.get(BOX_CSRF_HEADER, "")
            )
    result = await asyncio.to_thread(service.create_admin_session, body.management_key, previous)
    await _register_owner(server, service)
    _set_browser_cookie(response, request, service, result)
    return BrowserSessionResponse.model_validate(result)


def _set_browser_cookie(
    response: Response, request: Request, service: BoxAccountService, result: dict[str, object]
) -> None:
    response.set_cookie(
        browser_cookie_name(service.box_id),
        str(result["access_token"]),
        max_age=int(str(result["expires_in"])),
        path="/api",
        secure=request.url.scheme == "https",
        httponly=True,
        samesite="strict",
    )
    response.headers["Cache-Control"] = "no-store"


@auth_router.get(
    "/browser/session",
    response_model=BrowserSessionResponse,
    openapi_extra=box_auth_metadata(admin_only=True, mutation=False),
    summary="Read the current browser session and CSRF token",
)
async def browser_session(
    response: Response, context: Context, service: Service
) -> BrowserSessionResponse:
    response.headers["Cache-Control"] = "no-store"
    return BrowserSessionResponse.model_validate(
        await asyncio.to_thread(service.browser_session, context)
    )


@auth_router.post(
    "/browser/refresh",
    response_model=BrowserSessionResponse,
    openapi_extra=box_auth_metadata(admin_only=True, mutation=True),
    summary="Renew the current Web admin cookie and revoke its previous session",
)
async def refresh_browser_session(
    request: Request, response: Response, context: Context, service: Service
) -> BrowserSessionResponse:
    result = await asyncio.to_thread(service.refresh_admin_session, context)
    _set_browser_cookie(response, request, service, result)
    return BrowserSessionResponse.model_validate(result)


@auth_router.delete(
    "/browser/session",
    status_code=204,
    openapi_extra=box_auth_metadata(admin_only=True, mutation=True),
    summary="Revoke the current Web admin session",
)
async def delete_browser_session(context: Context, service: Service) -> Response:
    await asyncio.to_thread(service.browser_session, context)
    await asyncio.to_thread(service.logout, context)
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    response.delete_cookie(
        browser_cookie_name(service.box_id), path="/api", httponly=True, samesite="strict"
    )
    return response


@router.get(
    "/account",
    response_model=BoxAccountResponse,
    openapi_extra=box_auth_metadata(admin_only=False, mutation=False),
    summary="Read the current box account and authority",
)
async def account(
    context: Context, service: Service, server: OctopServer = Depends(get_server)
) -> BoxAccountResponse:
    result = await asyncio.to_thread(service.account, context)
    assert server.services is not None
    instance = await asyncio.to_thread(local_instance, server.services)
    result.update(
        instance_id=instance,
        account_id=hashlib.sha256(f"{instance}:{context.user.id}".encode()).hexdigest(),
    )
    return BoxAccountResponse.model_validate(result)


@router.get(
    "/clients",
    response_model=list[PairedClientResponse],
    openapi_extra=box_auth_metadata(admin_only=False, mutation=False),
    summary="List paired clients and their revocation state",
)
async def clients(context: Context, service: Service) -> list[PairedClientResponse]:
    return [
        PairedClientResponse.model_validate(item)
        for item in await asyncio.to_thread(service.clients, context)
    ]


@router.post(
    "/pairing-grants",
    response_model=GrantResponse,
    openapi_extra=box_auth_metadata(admin_only=False, mutation=True),
    summary="Create a one-use guest share",
    description="Creates a one-use share valid for 48 hours (172800 seconds). Web admins use the current cookie and CSRF token. Primary devices additionally sign action=enroll with params.request_id. A proved candidate becomes an independent guest immediately.",
)
async def enroll(
    body: GrantRequest, response: Response, context: Context, service: Service
) -> GrantResponse:
    response.headers["Cache-Control"] = "no-store"
    return GrantResponse.model_validate(
        await asyncio.to_thread(
            service.create_enroll_grant, context, proof=body.proof, request_id=body.request_id
        )
    )


@router.get(
    "/pairings",
    openapi_extra=box_auth_metadata(admin_only=False, mutation=False),
    summary="Retired approval queue",
    status_code=410,
)
@router.post(
    "/pairings/{pairing_id}/approve",
    openapi_extra=box_auth_metadata(admin_only=False, mutation=True),
    summary="Retired candidate approval",
    status_code=410,
)
@router.post(
    "/pairings/{pairing_id}/cancel",
    openapi_extra=box_auth_metadata(admin_only=False, mutation=True),
    summary="Retired candidate approval cancellation",
    status_code=410,
)
async def retired_approval(context: Context) -> Response:
    raise OctopError.localized(
        ErrorCode.FORBIDDEN, status=410, details={"reason": "box_approval_retired"}
    )


@router.post(
    "/clients/{client_id}/revoke",
    status_code=204,
    openapi_extra=box_auth_metadata(admin_only=True, mutation=True),
    summary="Revoke any native client and its sessions",
    description="Web administrator only. Revocation may remove the last owner device; existing data remains.",
)
async def revoke(client_id: str, context: Context, service: Service) -> Response:
    await asyncio.to_thread(service.revoke_client, context, client_id)
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.post(
    "/clients/{client_id}/primary",
    status_code=410,
    openapi_extra=box_auth_metadata(admin_only=True, mutation=True),
    summary="Retired exclusive primary-device transfer",
    description="Returns 410 without changing authorization. Use /owner to explicitly grant shared owner access.",
    deprecated=True,
)
async def set_primary(client_id: str, context: Context, service: Service) -> Response:
    await asyncio.to_thread(service.set_primary, context, client_id)
    return Response(status_code=410)


@router.post(
    "/clients/{client_id}/owner",
    status_code=204,
    openapi_extra=box_auth_metadata(admin_only=True, mutation=True),
    summary="Grant an active client access to the owner account",
    description="Web administrator only. Other owner devices retain access. This client must sign in again; its previous guest data remains separate.",
)
async def add_owner_device(client_id: str, context: Context, service: Service) -> Response:
    await asyncio.to_thread(service.add_owner_device, context, client_id)
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.post(
    "/management-key/rotate",
    response_model=RotateKeyResponse,
    openapi_extra=box_auth_metadata(admin_only=True, mutation=True),
    summary="Rotate the management key and revoke every Web admin session",
)
async def rotate_key(
    body: RotateKeyRequest, response: Response, context: Context, service: Service
) -> RotateKeyResponse:
    result = await asyncio.to_thread(
        service.rotate_management_key, context, revoke_clients=body.revoke_clients
    )
    response.headers["Cache-Control"] = "no-store"
    response.delete_cookie(
        browser_cookie_name(service.box_id), path="/api", httponly=True, samesite="strict"
    )
    return RotateKeyResponse.model_validate(result)
