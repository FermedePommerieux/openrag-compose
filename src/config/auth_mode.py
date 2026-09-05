"""Deployment authentication policy; never infer a fallback for an explicit mode."""

import os

_onboarding_local_enabled = False


def get_deployment_auth_mode() -> str:
    mode = os.getenv("OPENRAG_AUTH_MODE", "auto").lower().strip()
    if mode not in {"auto", "local", "local_plus_external", "external", "no_auth"}:
        raise ValueError("Invalid OPENRAG_AUTH_MODE")
    return mode


def set_onboarding_local_auth(enabled: bool) -> None:
    """Publish the committed setup choice; loaded before serving requests."""
    global _onboarding_local_enabled
    _onboarding_local_enabled = enabled


def onboarding_local_auth_active() -> bool:
    return get_deployment_auth_mode() == "auto" and _onboarding_local_enabled


def get_auth_mode() -> str:
    # Explicit deployment policies override the first-run choice. Mixed mode
    # preserves optional Google login when the operator has configured it.
    return "local_plus_external" if onboarding_local_auth_active() else get_deployment_auth_mode()


def local_auth_enabled() -> bool:
    return get_auth_mode() in {"local", "local_plus_external"}


def secure_auth_cookie() -> bool:
    return os.getenv("OPENRAG_AUTH_COOKIE_SECURE", "true").lower() not in {"false", "0", "no"}


def google_login_enabled() -> bool:
    from config import settings

    return (
        get_auth_mode() in {"auto", "local_plus_external", "external"}
        and not settings.IBM_AUTH_ENABLED
        and bool(settings.GOOGLE_OAUTH_CLIENT_ID and settings.GOOGLE_OAUTH_CLIENT_SECRET)
    )


def validate_auth_configuration() -> None:
    from config import settings
    from services.rbac_service import is_rbac_enforced

    mode = get_auth_mode()
    if local_auth_enabled():
        validate_local_auth_prerequisites()
        if not is_rbac_enforced():
            raise RuntimeError("Local authentication requires OPENRAG_RBAC_ENFORCE=true")
    if mode == "external" and not (settings.IBM_AUTH_ENABLED or google_login_enabled()):
        raise RuntimeError("External authentication requested without a configured provider")
    if mode == "no_auth" and settings.IBM_AUTH_ENABLED:
        raise RuntimeError("Explicit no-auth cannot be combined with IBM gateway authentication")


def validate_local_auth_prerequisites() -> None:
    from config import settings

    if settings.IBM_AUTH_ENABLED:
        raise RuntimeError("Local authentication cannot use the IBM gateway auth mode")
    if settings.is_dev_role_toggle_enabled():
        raise RuntimeError("Disable the development role toggle for local authentication")
    if os.getenv("OPENRAG_DB_ECHO", "false").lower() in {"true", "1", "yes"}:
        raise RuntimeError("SQL echo must be disabled for credential storage")


def public_auth_configuration() -> dict:
    return {
        "auth_mode": get_auth_mode(),
        "local_auth_enabled": local_auth_enabled(),
        "google_auth_enabled": google_login_enabled(),
    }
