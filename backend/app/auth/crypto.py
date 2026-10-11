"""Pure authentication primitives: password hashing and JWT encoding.

Infrastructure/cross-cutting logic with no web-framework or database
dependency (spec §3.5 / D3: "JWT 编解码、密码哈希等纯逻辑保留在 auth").
Keeping it free of FastAPI lets the Auth Service perform credential
verification without importing the API-layer `security` module, so both layers
can share one implementation.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt

from backend.app.config import settings


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain password against a bcrypt hash."""
    return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))


def get_password_hash(password: str) -> str:
    """Generate bcrypt hash for a plain password."""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Encode a JWT access token with expiration."""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


__all__ = ["verify_password", "get_password_hash", "create_access_token"]
