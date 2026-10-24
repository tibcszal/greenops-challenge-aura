"""EXO2-HMAC-SHA256 request signing.

EXO2 is the Exoscale API signature scheme, version 2. The secret never travels:
both sides hash the request line with it and compare. Sign the complete prefixed
path, including /runs/{id}/..., or the signature will not match.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from urllib.parse import urlparse

DEFAULT_TTL_SECONDS = 60


def authorization(key: str, secret: str, method: str, url: str,
                  ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
    """Authorization header value for an unparameterized GET-style request.

    The signed message is the request line, three empty lines reserved for body
    parameters, query parameters and headers, then the expiry timestamp.
    """
    expires = str(int(time.time()) + ttl_seconds)
    path = urlparse(url).path
    message = "\n".join([f"{method.upper()} {path}", "", "", "", expires])
    digest = hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()
    signature = base64.b64encode(digest).decode()
    return (f"EXO2-HMAC-SHA256 credential={key},expires={expires},"
            f"signature={signature}")
