from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest

from src.core.config import get_settings
from src.core.exceptions import AuthenticationError
from src.core.security import (
    create_access_token,
    decode_access_token,
    hash_opaque_token,
    hash_password,
    verify_password,
)


def test_password_hash_is_verifiable() -> None:
    hashed_password = hash_password("Correct-Horse-Battery-Staple")

    assert verify_password("Correct-Horse-Battery-Staple", hashed_password)
    assert not verify_password("incorrect-password", hashed_password)


def test_opaque_token_hash_is_deterministic() -> None:
    assert hash_opaque_token("token-value") == hash_opaque_token("token-value")
    assert hash_opaque_token("token-value") != hash_opaque_token("another-token")


def test_access_token_round_trip() -> None:
    user_id, session_id = uuid4(), uuid4()

    token, expires_in = create_access_token(user_id, session_id)

    assert expires_in == get_settings().access_token_minutes * 60
    assert decode_access_token(token) == (user_id, session_id)


def test_tampered_access_token_is_rejected() -> None:
    token, _ = create_access_token(uuid4(), uuid4())
    tampered = token[:-3] + ("aaa" if not token.endswith("aaa") else "bbb")

    with pytest.raises(AuthenticationError):
        decode_access_token(tampered)


def test_access_token_without_required_claims_is_rejected() -> None:
    settings = get_settings()
    now = datetime.now(UTC)
    # Firma válida pero sin jti/iat/nbf: la validación estricta debe rechazarlo.
    incomplete = jwt.encode(
        {
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "sub": str(uuid4()),
            "sid": str(uuid4()),
            "exp": now + timedelta(minutes=5),
            "typ": "access",
        },
        settings.jwt_secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )

    with pytest.raises(AuthenticationError):
        decode_access_token(incomplete)


def test_access_token_with_wrong_audience_is_rejected() -> None:
    settings = get_settings()
    now = datetime.now(UTC)
    foreign = jwt.encode(
        {
            "iss": settings.jwt_issuer,
            "aud": "another-app",
            "sub": str(uuid4()),
            "sid": str(uuid4()),
            "jti": str(uuid4()),
            "iat": now,
            "nbf": now,
            "exp": now + timedelta(minutes=5),
            "typ": "access",
        },
        settings.jwt_secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )

    with pytest.raises(AuthenticationError):
        decode_access_token(foreign)
