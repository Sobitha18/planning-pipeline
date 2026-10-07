"""Auth service: wraps JwtValidator for request-scoped verification."""

from auth.jwt import JwtValidator
from auth.does_not_exist import Thing


class JwtValidatorPlus(JwtValidator):
    """Adds request-scoped caching on top of JwtValidator."""

    def verify_request(self, token: str) -> dict:
        validator = JwtValidator()
        return validator.verify(token)


def ambiguous_caller():
    """Calls parse_claims by bare name with no import — ambiguous between
    helpers.py and token_utils.py, which both define it."""
    return parse_claims()
