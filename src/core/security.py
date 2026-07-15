from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import token_urlsafe
from uuid import UUID

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


def create_access_token(user_id: UUID, session_id: UUID) -> str:
    settings = get_settings()
    expires_at = datetime.now(UTC) + timedelta(minutes=settings.access_token_minutes)
    return jwt.encode(
        {"sub": str(user_id), "sid": str(session_id), "type": "access", "exp": expires_at},
        settings.jwt_secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )


def create_refresh_token() -> tuple[str, str, datetime]:
    settings = get_settings()
    token = token_urlsafe(48)
    expires_at = datetime.now(UTC) + timedelta(days=settings.refresh_token_days)
    return token, hash_opaque_token(token), expires_at


def hash_opaque_token(token: str) -> str:
    return sha256(token.encode("utf-8")).hexdigest()


def decode_access_token(token: str) -> tuple[UUID, UUID]:
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
        )
        if payload.get("type") != "access":
            raise AuthenticationError("Invalid token type.")
        return UUID(str(payload["sub"])), UUID(str(payload["sid"]))
    except (InvalidTokenError, KeyError, ValueError) as error:
        raise AuthenticationError("Invalid or expired access token.") from error
