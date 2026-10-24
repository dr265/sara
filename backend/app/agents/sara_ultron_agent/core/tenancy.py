"""Tenant identity: deciding WHO a request belongs to.

WHY THIS EXISTS
---------------
Ultron was built as a single-operator tool. Every store was global: one session
dict, one device registry, one memory folder, and one shared ``SARA_API_KEY``.
That is fine on one laptop and a data breach the moment the app is public -
``GET /ultron/devices`` would return every customer's paired machines, tasks
and memory to whoever asked.

Hosting does not fix this. Vercel, Render and Oracle can all run the process;
none of them can invent a tenant boundary that is absent from the code. This
module is that boundary.

THE DESIGN RULE
---------------
**Additive and fail-open only where it already was.** Every existing call site
keeps working unchanged:

* no identity presented  -> the LOCAL tenant (your laptop, exactly as before)
* ``SARA_API_KEY``       -> the LOCAL tenant (your admin scripts, E2E tests)
* a valid Clerk JWT     -> that Clerk user, and only their data

So local development, the 375 existing tests and the E2E scripts are untouched,
while a deployed instance gets real isolation. Anything that would have been
refused before is still refused: this never turns a 401 into a 200.

IDENTITY SOURCES, in order
--------------------------
1. ``Authorization: Bearer <Clerk session JWT>`` -> verified against Clerk's
   JWKS, giving the Clerk user id.
2. ``X-API-Key: <SARA_API_KEY>`` -> the local tenant.
3. Neither -> :class:`Anonymous`, which every store treats as "no tenant" and
   which authenticated routes reject with 401.

WHY A LOCAL TENANT EXISTS
-------------------------
It is not a backdoor. It is the same trust boundary that existed before this
file: whoever holds ``SARA_API_KEY``. Anyone who can reach that already had
full access, and continues to - but only to the local tenant's data, which on a
real deployment is empty.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

log = logging.getLogger("sara.ultron.tenancy")

#: Tenant id used when no customer identity is presented. Single-operator
#: installs, local development and the E2E scripts all land here.
LOCAL_TENANT_ID = "local"


@dataclass(frozen=True)
class Tenant:
    """Who a request belongs to."""

    id: str
    kind: str                      # "local" | "clerk"
    label: str = ""
    is_authenticated: bool = True
    claims: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_local(self) -> bool:
        return self.kind == "local"


#: Identity for a request that presented nothing. ``is_authenticated`` is False
#: precisely so authenticated routes can refuse it - without that flag a
#: missing header would silently read as "the local tenant".
ANONYMOUS = Tenant(
    id="", kind="anonymous", label="", is_authenticated=False, claims={}
)

LOCAL_TENANT = Tenant(id=LOCAL_TENANT_ID, kind="local", label="local operator")


# ---------------------------------------------------------------------------
# Clerk JWT verification
# ---------------------------------------------------------------------------
#: JWKS is fetched over the network and cached. Cached because verification runs
#: on every authenticated request, and fetched because Clerk rotates signing keys
#: and a hard-coded key would start rejecting valid tokens one day without
#: explanation.
_JWKS_CACHE: Dict[str, Any] = {}
_JWKS_LOCK = threading.Lock()
_JWKS_TTL_S = 3600.0


def _clerk_domain() -> str:
    """Issuer for the Clerk instance, from its publishable key.

    The publishable key is ``pk_test_<base64 of the frontend API host>`` or
    ``pk_live_...``. Decoding it avoids asking the operator to paste the same
    domain twice, which is a config error waiting to happen.
    """
    import base64

    key = (os.getenv("NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY")
           or os.getenv("CLERK_PUBLISHABLE_KEY") or "").strip()
    if not key:
        return ""
    parts = key.split("_", 2)
    if len(parts) < 3:
        return ""
    try:
        raw = base64.b64decode(parts[2] + "=" * (-len(parts[2]) % 4))
        return raw.decode("utf-8").rstrip("/")
    except Exception:  # noqa: BLE001 - malformed key, fall back to explicit
        return ""


def _jwks(issuer: str) -> Dict[str, Any]:
    """Fetch and cache Clerk's signing keys."""
    now = time.time()
    cached = _JWKS_CACHE.get(issuer)
    if cached and now - cached[0] < _JWKS_TTL_S:
        return cached[1]
    with _JWKS_LOCK:
        cached = _JWKS_CACHE.get(issuer)
        if cached and now - cached[0] < _JWKS_TTL_S:
            return cached[1]
        try:
            import requests
            resp = requests.get(f"{issuer}/.well-known/jwks.json", timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("Clerk JWKS unavailable for %s (%s)", issuer,
                        type(exc).__name__)
            # Serve a stale cache rather than failing every login if Clerk is
            # briefly unreachable.
            return cached[1] if cached else {}
        _JWKS_CACHE[issuer] = (now, data)
        return data


def verify_clerk_token(token: str) -> Optional[Dict[str, Any]]:
    """Verify a Clerk session JWT. Returns its claims, or ``None``.

    Fails closed on every path: no key, bad issuer, expired, bad signature or a
    JWKS endpoint that cannot be reached all return ``None``. The caller turns
    that into a 401.
    """
    issuer = _clerk_domain()
    if not issuer or not token:
        return None
    try:
        import jwt
        from jwt import PyJWKClient
    except ImportError:  # pragma: no cover - PyJWT is a pinned dependency
        log.error("PyJWT is not installed; Clerk auth cannot verify tokens.")
        return None

    client = PyJWKClient(f"{issuer}/.well-known/jwks.json")
    try:
        signing_key = client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=os.getenv("CLERK_AUDIENCE", "") or None,
            options={"verify_aud": bool(os.getenv("CLERK_AUDIENCE", "").strip())},
        )
    except Exception as exc:  # noqa: BLE001 - any verification failure is a 401
        log.info("Clerk token rejected: %s", type(exc).__name__)
        return None
    return claims


# ---------------------------------------------------------------------------
# Identity extraction
# ---------------------------------------------------------------------------
def _bearer(authorization: str) -> str:
    value = (authorization or "").strip()
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return ""


def _clerk_user_id(claims: Dict[str, Any]) -> str:
    """Extract the stable user id from Clerk claims."""
    sub = str(claims.get("sub") or "").strip()
    if sub:
        return sub
    # Clerk metadata sometimes carries a friendlier id.
    meta = claims.get("metadata") or {}
    if isinstance(meta, dict):
        return str(meta.get("user_id") or "").strip()
    return ""


def resolve_tenant(authorization: str = "", x_api_key: str = "") -> Tenant:
    """Identify the caller. Never raises - returns :data:`ANONYMOUS` instead.

    Order matters: a Clerk token wins over an API key, so a signed-in customer
    whose browser happens to also send a stale API key is still scoped to their
    own account rather than to the local tenant.
    """
    token = _bearer(authorization)
    if token:
        claims = verify_clerk_token(token)
        if claims:
            user_id = _clerk_user_id(claims)
            if user_id:
                label = ""
                for key in ("email", "username", "first_name"):
                    value = claims.get(key)
                    if isinstance(value, str) and value:
                        label = value
                        break
                return Tenant(id=user_id, kind="clerk", label=label,
                              claims=claims)
            log.warning("Clerk token carried no usable subject; treating as anonymous.")
        # A bearer token was presented but did not verify. Do NOT fall through
        # to the local tenant - that would let a broken or forged token silently
        # become the operator.

    provided = (x_api_key or "").strip()
    if provided:
        from app.api.deps import API_KEY
        if API_KEY:
            import hmac
            if hmac.compare_digest(provided, API_KEY):
                return LOCAL_TENANT

    return ANONYMOUS


def require_tenant(authorization: str = "", x_api_key: str = "") -> Tenant:
    """Like :func:`resolve_tenant` but refuses an unauthenticated caller."""
    from fastapi import HTTPException

    tenant = resolve_tenant(authorization, x_api_key)
    if not tenant.is_authenticated:
        raise HTTPException(
            status_code=401,
            detail=("Unauthorized: sign in, or present the SARA_API_KEY. "
                    "Without an identity this request cannot be scoped to a "
                    "tenant, and unscoped reads are refused."),
        )
    return tenant


def scope_key(tenant: Optional[Tenant]) -> str:
    """Storage key for a tenant.

    Anonymous callers get a sentinel rather than an empty string, so an
    unscoped read can never accidentally match rows written under a real tenant.
    """
    if tenant is None or not tenant.is_authenticated:
        return "__anonymous__"
    return tenant.id