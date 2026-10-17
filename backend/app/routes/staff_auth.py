"""Staff Authentication Routes.

Thin HTTP layer: request parsing, token minting and response shaping only.
Credential checks are delegated to
:class:`backend.app.service.auth.AuthService`; domain exceptions raised there
are mapped to HTTP status codes by the global exception handler.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth.security import create_access_token, get_current_staff
from backend.app.config import settings
from backend.app.database import get_db
from backend.app.models.user import StaffUser
from backend.app.schemas.auth import StaffLoginRequest, StaffResponse, TokenResponse
from backend.app.service.auth import AuthService

router = APIRouter(prefix="/staff", tags=["Staff Auth"])


@router.post("/login", response_model=TokenResponse)
async def staff_login(payload: StaffLoginRequest, db: AsyncSession = Depends(get_db)):
    """Authenticate staff member by username and password."""
    staff = await AuthService.authenticate_staff(
        payload.username, payload.password, session=db
    )
    token = create_access_token(
        data={
            "sub": str(staff.id),
            "type": "staff",
            "username": staff.username,
            "role": staff.role,
            "branch_id": staff.branch_id,
        }
    )
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        user={
            "id": staff.id,
            "username": staff.username,
            "role": staff.role,
            "branch_id": staff.branch_id,
            "type": "staff",
        },
    )


@router.get("/login-hints")
def staff_login_hints():
    """Public: which demo account set the active database contains.

    The development database is seeded with ``admin`` / ``downtown_staff``;
    the isolated evaluation database carries ``eval_admin`` / ``eval_staff``.
    The login UI asks this endpoint instead of assuming the development seed.
    """
    return {"dataset": settings.dataset}


@router.get("/me", response_model=StaffResponse)
async def get_staff_me(current_staff: StaffUser = Depends(get_current_staff)):
    """Return currently logged-in staff profile."""
    return current_staff
