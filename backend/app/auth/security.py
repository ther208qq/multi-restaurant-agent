"""Security utilities: JWT decoding and FastAPI auth dependencies.

The pure password-hashing / JWT-encoding primitives live in
:mod:`backend.app.auth.crypto` and are re-exported here so existing callers keep
importing them from this module unchanged.

Token-shape checks stay here; loading the user behind a token is delegated to
:class:`backend.app.service.auth.AuthService`, so this module performs no DB
access of its own (spec §3.5 / D3).
"""

from typing import Optional

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth.crypto import (
    create_access_token,
    get_password_hash,
    verify_password,
)
from backend.app.config import settings
from backend.app.database import get_db
from backend.app.models.user import Customer, StaffUser
from backend.app.service.auth import AuthService

security_bearer = HTTPBearer(auto_error=False)


def decode_access_token(token: str) -> dict:
    """Decode and validate a JWT access token."""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        return payload
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无法验证凭据",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def get_current_customer(
    auth: Optional[HTTPAuthorizationCredentials] = Depends(security_bearer),
    db: AsyncSession = Depends(get_db),
) -> Customer:
    """Dependency: Extract authenticated customer from JWT Bearer token."""
    if not auth:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="需要身份验证令牌",
        )
    payload = decode_access_token(auth.credentials)
    user_type = payload.get("type")
    customer_id = payload.get("sub")

    if user_type != "customer" or not customer_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="顾客令牌无效",
        )

    return await AuthService.get_customer(int(customer_id), session=db)


async def get_current_staff(
    auth: Optional[HTTPAuthorizationCredentials] = Depends(security_bearer),
    db: AsyncSession = Depends(get_db),
) -> StaffUser:
    """Dependency: Extract authenticated staff member from JWT Bearer token."""
    if not auth:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="需要员工身份验证令牌",
        )
    payload = decode_access_token(auth.credentials)
    user_type = payload.get("type")
    staff_id = payload.get("sub")

    if user_type != "staff" or not staff_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="员工令牌无效",
        )

    return await AuthService.get_staff(int(staff_id), session=db)


def require_admin(staff: StaffUser = Depends(get_current_staff)) -> StaffUser:
    """Enforce admin role."""
    if staff.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限",
        )
    return staff


__all__ = [
    "security_bearer",
    "verify_password",
    "get_password_hash",
    "create_access_token",
    "decode_access_token",
    "get_current_customer",
    "get_current_staff",
    "require_admin",
]
