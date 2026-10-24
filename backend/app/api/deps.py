import hmac

from fastapi import Depends, Header, HTTPException

from app.core.config import settings

# Stable per-install key, expected in backend/.env (SARA_API_KEY).
API_KEY = (settings.SARA_API_KEY or "").strip()


def require_api_key(
    x_api_key: str = Header(default=""),
    authorization: str = Header(default=""),
) -> None:
    """Reject requests that do not present the configured API key."""
    if not API_KEY:
        raise HTTPException(
            status_code=503,
            detail="SARA_API_KEY is not configured on the backend. Add it to backend/.env.",
        )
    provided = x_api_key.strip()
    if not provided and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()
    if not provided or not hmac.compare_digest(provided, API_KEY):
        raise HTTPException(status_code=401, detail="Unauthorized: missing or invalid API key")


# ---------------------------------------------------------------------------
# Tenant-aware authentication
# ---------------------------------------------------------------------------
def current_tenant(
    x_api_key: str = Header(default=""),
    authorization: str = Header(default=""),
):
    """Identify the caller as a tenant.

    This is the SaaS-aware sibling of :func:`require_api_key` and is
    intentionally **additive**: ``require_api_key`` stays exactly as it was and
    every route already using it keeps working unchanged.

    Routes that read or write tenant data must depend on THIS instead, because
    knowing *that* a caller is authorised is not the same as knowing *whose*
    data they may see. The shared key authenticates; this scopes.
    """
    from app.agents.sara_ultron_agent.core.tenancy import require_tenant

    return require_tenant(authorization=authorization, x_api_key=x_api_key)


def optional_tenant(
    x_api_key: str = Header(default=""),
    authorization: str = Header(default=""),
):
    """Same as :func:`current_tenant` but never refuses.

    For read-only status surfaces that should still render something signed out.
    Returns an unauthenticated tenant, and the caller must decide what an
    unauthenticated tenant is allowed to see - which for tenant data is nothing.
    """
    from app.agents.sara_ultron_agent.core.tenancy import resolve_tenant

    return resolve_tenant(authorization=authorization, x_api_key=x_api_key)