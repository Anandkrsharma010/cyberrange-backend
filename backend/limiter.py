from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from backend.config import get_settings

settings = get_settings()


def _get_real_client_ip(request: Request) -> str:
    """
    Returns the real client IP for rate limiting.

    If the direct connection comes from a trusted proxy IP, extract the
    leftmost (original client) address from X-Forwarded-For.
    Otherwise fall back to the direct connection IP.
    """
    trusted_proxies = settings.TRUSTED_PROXY_IPS

    if trusted_proxies:
        direct_ip = request.client.host if request.client else None

        if direct_ip in trusted_proxies:
            forwarded_for = request.headers.get("X-Forwarded-For", "")
            if forwarded_for:
                client_ip = forwarded_for.split(",")[0].strip()
                if client_ip:
                    return client_ip

    return get_remote_address(request)


limiter = Limiter(key_func=_get_real_client_ip, headers_enabled=True)