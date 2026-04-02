"""JWT-based authentication for WebSocket terminal access."""

from __future__ import annotations

import logging
import time
from typing import Optional

import jwt

logger = logging.getLogger(__name__)

_ALGORITHM = "HS256"
_ISSUER = "supersandbox"


def create_terminal_token(
    secret: str,
    sandbox_id: str,
    ttl_seconds: int = 300,
) -> tuple[str, int]:
    """Create a signed JWT token scoped to a specific sandbox terminal session.

    Returns:
        Tuple of (token_string, expiry_unix_timestamp).
    """
    now = int(time.time())
    exp = now + ttl_seconds
    payload = {
        "sub": sandbox_id,
        "iss": _ISSUER,
        "iat": now,
        "exp": exp,
    }
    token = jwt.encode(payload, secret, algorithm=_ALGORITHM)
    return token, exp


def validate_terminal_token(
    secret: str,
    token: str,
    expected_sandbox_id: str,
) -> Optional[str]:
    """Validate a terminal JWT token.

    Returns:
        None if valid, or an error message string if invalid.
    """
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[_ALGORITHM],
            issuer=_ISSUER,
            options={"require": ["sub", "exp", "iat", "iss"]},
        )
    except jwt.ExpiredSignatureError:
        return "Token has expired"
    except jwt.InvalidIssuerError:
        return "Invalid token issuer"
    except jwt.DecodeError:
        return "Invalid token"
    except jwt.InvalidTokenError as e:
        return f"Token validation failed: {e}"

    if payload.get("sub") != expected_sandbox_id:
        return "Token not valid for this sandbox"

    return None
