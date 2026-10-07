"""Tests for JwtValidator token verification."""
from src.auth.jwt import JwtValidator


def test_verify_returns_dict():
    validator = JwtValidator()
    assert validator.verify("token") == {}
