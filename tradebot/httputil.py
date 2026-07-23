"""HTTP client helpers — CA bundles and OS trust store (no verify=False)."""
from __future__ import annotations

import os
import ssl
from typing import Any


_TLS_HINT = (
    "TLS interception detected? truststore is active; "
    "set PULSE_CA_BUNDLE to your proxy's CA if this persists."
)


def inject_os_trust_store() -> str | None:
    """Inject the OS certificate store into ssl. Returns a warning, or None."""
    try:
        import truststore
        truststore.inject_into_ssl()
        return None
    except Exception as e:
        return f"OS trust store not injected ({e}); using default certs"


def httpx_client_kwargs(**extra: Any) -> dict[str, Any]:
    """Kwargs for httpx clients. Honors PULSE_CA_BUNDLE; never disables verify."""
    kwargs: dict[str, Any] = {"timeout": 10}
    kwargs.update(extra)
    bundle = (os.environ.get("PULSE_CA_BUNDLE") or "").strip()
    if bundle:
        kwargs["verify"] = bundle
    return kwargs


def is_ssl_error(exc: BaseException) -> bool:
    if isinstance(exc, ssl.SSLError):
        return True
    name = type(exc).__name__.lower()
    if "ssl" in name or "certificate" in name:
        return True
    msg = str(exc).lower()
    return any(s in msg for s in (
        "ssl", "certificate", "cert verify", "certificate_verify_failed",
    ))


def with_ssl_hint(message: str, exc: BaseException | None = None) -> str:
    if exc is not None and is_ssl_error(exc):
        return f"{message} {_TLS_HINT}"
    if exc is None and is_ssl_error(ValueError(message)):
        return f"{message} {_TLS_HINT}"
    # also scan the message itself (wrapped httpx errors)
    if is_ssl_error(Exception(message)):
        return f"{message} {_TLS_HINT}"
    return message
