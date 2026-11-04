"""API the GitHub Actions runner calls, authenticated with its OIDC token.

Every handler re-checks, from the token's claims alone, that the caller is the
right repo, workflow, event, run and (for gated tools) environment.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from mcp import types
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .audit import audit
from .oidc import OIDCError, RunnerIdentity
from .policy import Limits
from .store import ServerRecord

router = APIRouter(prefix="/runner")

MANIFEST_EVENTS = {"push", "workflow_dispatch"}
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
ENV_NAME = re.compile(r"^(?!\.+$)[A-Za-z0-9_.\- ]{1,255}$")  # not "." or "..": they'd change the API path
TOOL_NAME = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")  # MCP tool name charset
CALL_ID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
MAX_TOOLS = 256
RESULT_ENVELOPE_BYTES = 64 * 1024


class LimitsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    max_calls_per_hour: int | None = Field(default=None, gt=0)
    max_concurrent_calls: int | None = Field(default=None, gt=0)
    max_argument_bytes: int | None = Field(default=None, gt=0)
    max_result_bytes: int | None = Field(default=None, gt=0)
    call_timeout_seconds: int | None = Field(default=None, gt=0)
    approval_timeout_seconds: int | None = Field(default=None, gt=0)


class PolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limits: LimitsModel = Field(default_factory=LimitsModel)


class Manifest(BaseModel):
    # Unknown fields are rejected, like gh-aw's DisallowUnknownFields: a field the
    # gateway doesn't understand must never be silently ignored.
    model_config = ConfigDict(extra="forbid")
    server_name: str | None = Field(default=None, max_length=200)
    server_version: str | None = Field(default=None, max_length=100)
    instructions: str | None = Field(default=None, max_length=20_000)
    tools: list[dict[str, Any]] = Field(default_factory=list, max_length=MAX_TOOLS)
    approval_required: list[str] = Field(default_factory=list)
    approval_environment: str = "mcp-approval"
    policy: PolicyModel = Field(default_factory=PolicyModel)
    client_token_sha256: str | None = None

    @field_validator("tools")
    @classmethod
    def _tools(cls, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        names = []
        for tool in tools:
            types.Tool.model_validate(tool)
            if not TOOL_NAME.match(str(tool.get("name", ""))):
                raise ValueError(f"invalid tool name {tool.get('name')!r}")
            names.append(tool["name"])
        if len(set(names)) != len(names):
            raise ValueError("duplicate tool names")
        return tools


class ResultBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result: dict[str, Any]


def runner_identity(request: Request) -> RunnerIdentity:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "Missing OIDC bearer token")
    try:
        return request.app.state.oidc.verify(auth[7:].strip())
    except OIDCError as exc:
        audit("runner.auth_failed", reason=str(exc), ip=request.client.host if request.client else None)
        raise HTTPException(401, str(exc)) from exc


def _clamp_warnings(policy: PolicyModel, ceilings: Limits) -> list[str]:
    """Limits above the gateway's caps are lowered (never raised), with a warning."""
    warnings = []
    for name, value in policy.limits.model_dump(exclude_none=True).items():
        cap = getattr(ceilings, name)
        if value > cap:
            warnings.append(f"limits.{name}={value} is above this gateway's cap; using {cap}")
    return warnings


@router.put("/manifest")
async def put_manifest(request: Request, ident: RunnerIdentity = Depends(runner_identity)) -> dict[str, Any]:
    if ident.event_name not in MANIFEST_EVENTS:
        raise HTTPException(403, f"Manifests can only be uploaded from {sorted(MANIFEST_EVENTS)} events")
    allowed = request.app.state.settings.allowed_owners
    if allowed and ident.repository.split("/", 1)[0].lower() not in allowed:
        audit("manifest.rejected", repo=ident.repository, reason="owner not allowed")
        raise HTTPException(403, "This gateway does not accept servers from this owner")

    raw = await request.body()
    if len(raw) > request.app.state.settings.max_manifest_bytes:
        raise HTTPException(413, "Manifest too large")
    try:
        manifest = Manifest.model_validate(json.loads(raw))
    except (ValueError, ValidationError) as exc:
        raise HTTPException(422, f"Invalid manifest: {exc}") from exc
    if manifest.client_token_sha256 and not SHA256_HEX.match(manifest.client_token_sha256):
        raise HTTPException(422, "client_token_sha256 must be a lowercase sha256 hex digest")
    unknown = set(manifest.approval_required) - {t.get("name") for t in manifest.tools}
    if unknown:
        raise HTTPException(422, f"approval_required lists unknown tools: {sorted(unknown)}")
    if manifest.approval_required and not ENV_NAME.match(manifest.approval_environment):
        raise HTTPException(422, "approval_environment is not a valid GitHub Environment name")
    warnings = _clamp_warnings(manifest.policy, request.app.state.broker.ceilings)

    store = request.app.state.store
    existing = store.get_server(ident.repository_id)
    # The first upload pins the branch and the workflow file. Uploads from another
    # branch or workflow are refused, so a feature branch (or an unrelated workflow
    # with id-token: write) can't replace the live tools or the client token.
    if existing and existing.ref != ident.ref:
        audit("manifest.rejected", repo=ident.repository, reason="ref mismatch", ref=ident.ref)
        raise HTTPException(
            409, f"This server is registered from {existing.ref}; refusing a manifest from {ident.ref}"
        )
    if existing and existing.workflow_path != ident.workflow_path:
        audit("manifest.rejected", repo=ident.repository, reason="workflow mismatch", workflow=ident.workflow_path)
        raise HTTPException(
            409, f"This server is registered from {existing.workflow_path}; "
                 f"refusing a manifest from {ident.workflow_path}"
        )
    # Tool-call runs are workflow_dispatch runs too. A dispatch upload is accepted
    # only if GitHub confirms it is a manual manifest run, so a token taken from a
    # tool-call job can't re-register the server.
    dispatcher = request.app.state.broker.dispatcher
    candidate = existing or ServerRecord(
        ident.repository_id, ident.repository, ident.workflow_path, ident.ref, None, None
    )
    if ident.event_name == "workflow_dispatch":
        if not await dispatcher.verify_manifest_run(candidate, ident.run_id):
            audit("manifest.rejected", repo=ident.repository, reason="not a manifest run", run_id=ident.run_id)
            raise HTTPException(403, "workflow_dispatch manifest uploads must come from a run with no call_id")
    if not existing and not manifest.client_token_sha256:
        raise HTTPException(422, "First registration needs MCP_CLIENT_TOKEN set as a repo secret")
    if not existing:
        # Only repos that installed the gateway's GitHub App can register, so any
        # repo on GitHub can't fill the gateway with servers.
        try:
            installed = await dispatcher.is_installed(candidate)
        except httpx.HTTPError as exc:
            raise HTTPException(503, "Could not check the GitHub App installation; retry later") from exc
        if not installed:
            audit("manifest.rejected", repo=ident.repository, reason="app not installed")
            raise HTTPException(403, f"Install the MCP GitHub Runner app on {ident.repository} first")

    stored = manifest.model_dump(exclude={"client_token_sha256"})
    stored["policy"] = {"limits": manifest.policy.limits.model_dump(exclude_none=True)}
    store.upsert_server(
        repository_id=ident.repository_id,
        full_name=ident.repository,
        workflow_path=ident.workflow_path,
        ref=ident.ref,
        manifest=stored,
        token_hash=manifest.client_token_sha256,
    )
    audit(
        "manifest.registered", repo=ident.repository, run_id=ident.run_id, tools=len(manifest.tools),
        gated=manifest.approval_required, token_rotated=bool(manifest.client_token_sha256 and existing),
    )
    effective = request.app.state.broker.limits_for(store.get_server(ident.repository_id))
    return {
        "mcp_url": request.app.state.settings.mcp_url(ident.repository),
        "tools": len(manifest.tools),
        "limits": effective.__dict__,
        "warnings": warnings,
    }


def _deny(status: int, detail: str, ident: RunnerIdentity, call_id: str) -> HTTPException:
    audit("call.claim_rejected", repo=ident.repository, call_id=call_id, run_id=ident.run_id, status=status, reason=detail)
    return HTTPException(status, detail)


def _authorize_call(request: Request, call_id: str, ident: RunnerIdentity):
    """Ordered checks; the first failure decides the response."""
    if not CALL_ID.match(call_id):
        raise _deny(404, "Unknown call", ident, call_id)
    store = request.app.state.store
    call = store.get_call(call_id)
    # Same answer for "missing" and "someone else's call", so IDs can't be probed.
    if call is None or call.repository_id != ident.repository_id:
        raise _deny(404, "Unknown call", ident, call_id)
    if ident.event_name != "workflow_dispatch":
        raise _deny(403, "Calls can only be run from workflow_dispatch runs", ident, call_id)
    server = store.get_server(ident.repository_id)
    if server is None or server.workflow_path != ident.workflow_path:
        raise _deny(403, "This workflow is not the one registered for the server", ident, call_id)
    if ident.ref != server.ref:
        raise _deny(403, f"Calls run from {server.ref} only", ident, call_id)
    # Approval is enforced here, not in the workflow: GitHub only puts the
    # environment claim in the token once the job has passed the Environment's
    # protection rules (required reviewers).
    if call.approval_env and (ident.environment or "").lower() != call.approval_env.lower():
        raise _deny(
            403, f"Tool '{call.tool}' requires approval: the job must run in the '{call.approval_env}' environment",
            ident, call_id,
        )
    return call, server


@router.get("/calls/{call_id}")
async def get_call(call_id: str, request: Request, ident: RunnerIdentity = Depends(runner_identity)) -> dict[str, Any]:
    call, server = _authorize_call(request, call_id, ident)
    if call.status != "pending":
        raise _deny(409, f"Call is already {call.status}", ident, call_id)
    # Bind the call to the run GitHub started for it, so a token from any other
    # run of this workflow can't claim it.
    if not await request.app.state.broker.dispatcher.verify_run(server, call_id, ident.run_id):
        raise _deny(403, "This run was not started for this call", ident, call_id)
    store = request.app.state.store
    if not store.claim_call(call_id, ident.run_id):
        raise _deny(409, "Call was claimed by another run", ident, call_id)
    store.clear_arguments(call_id)
    limits = request.app.state.broker.limits_for(server)
    audit("call.claimed", repo=ident.repository, call_id=call_id, run_id=ident.run_id, environment=ident.environment)
    return {
        "call_id": call.id,
        "tool": call.tool,
        "arguments": call.arguments or {},
        "max_result_bytes": limits.max_result_bytes,
    }


@router.post("/calls/{call_id}/result")
async def post_result(call_id: str, request: Request, ident: RunnerIdentity = Depends(runner_identity)) -> dict[str, Any]:
    call, server = _authorize_call(request, call_id, ident)
    if call.status != "running" or call.run_id != ident.run_id:
        raise _deny(409, "This run did not claim the call, or it already finished", ident, call_id)

    limit = request.app.state.broker.limits_for(server).max_result_bytes + RESULT_ENVELOPE_BYTES
    raw = await request.body()
    if len(raw) > limit:
        raise _deny(413, f"Result too large ({len(raw)} bytes)", ident, call_id)
    try:
        body = ResultBody.model_validate(json.loads(raw))
        result = types.CallToolResult.model_validate(body.result)
    except (ValueError, ValidationError) as exc:
        raise HTTPException(422, f"Invalid CallToolResult: {exc}") from exc

    store = request.app.state.store
    store.finish_call(call_id, "done", from_status=("running",))
    delivered = request.app.state.broker.complete(
        call_id, result.model_dump(mode="json", by_alias=True, exclude_none=True)
    )
    audit("call.completed", repo=ident.repository, call_id=call_id, run_id=ident.run_id,
          is_error=bool(result.isError), delivered=delivered)
    return {"ok": True, "delivered": delivered}
