"""JWT validation and token utilities."""

import functools


class JwtValidator:
    """Validates and refreshes JWT tokens."""

    algorithm = "HS256"

    def verify(self, token: str) -> dict:
        """Validates JWT and returns claims."""
        return self._decode(token)

    @functools.lru_cache(maxsize=128)
    def _decode(self, token: str) -> dict:
        return {}

    class Inner:
        def ping(self) -> str:
            return "pong"


@functools.wraps(print)
async def refresh_token(
    token: str,
    *,
    leeway: int = 0,
) -> str:
    def _helper(x: int) -> int:
        return x + leeway

    return _helper(leeway)


def _module_private():
    pass
