from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import randbelow, token_urlsafe
from uuid import UUID, uuid4

import jwt
from jwt import InvalidTokenError
from pwdlib import PasswordHash

from src.core.config import get_settings
from src.core.exceptions import AuthenticationError

password_hash = PasswordHash.recommended()


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, hashed_password: str) -> bool:
    return password_hash.verify(password, hashed_password)


def create_access_token(user_id: UUID, session_id: UUID) -> tuple[str, int]:
    """Return a signed access token and its lifetime in seconds."""
    settings = get_settings()
    now = datetime.now(UTC)
    expires_in = settings.access_token_minutes * 60
    payload = {
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "sub": str(user_id),
        "sid": str(session_id),
        "jti": str(uuid4()),
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(seconds=expires_in),
        "typ": "access",
    }
    token = jwt.encode(
        payload,
        settings.jwt_secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )
    return token, expires_in


def create_refresh_token() -> tuple[str, str]:
    """Return an opaque refresh token and its hash; the expiry is decided per session."""
    token = token_urlsafe(48)
    return token, hash_opaque_token(token)


def hash_opaque_token(token: str) -> str:
    return sha256(token.encode("utf-8")).hexdigest()


def create_password_reset_code() -> tuple[str, str, datetime]:
    """Return a 6-digit recovery code, its hash, and its expiry."""
    settings = get_settings()
    code = f"{randbelow(1_000_000):06d}"
    expires_at = datetime.now(UTC) + timedelta(minutes=settings.reset_code_minutes)
    return code, hash_opaque_token(code), expires_at


def decode_access_token(token: str) -> tuple[UUID, UUID]:
    """Validate signature, issuer, audience, lifetime, and required claims."""
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            leeway=settings.jwt_leeway_seconds,
            options={"require": ["iss", "aud", "sub", "sid", "jti", "iat", "nbf", "exp"]},
        )
        if payload.get("typ") != "access":
            raise AuthenticationError("Invalid token type.")
        return UUID(str(payload["sub"])), UUID(str(payload["sid"]))
    except (InvalidTokenError, KeyError, ValueError) as error:
        raise AuthenticationError("Invalid or expired access token.") from error
