"""Customer Authentication Routes.

Thin HTTP layer: request parsing, token minting and response shaping only.
Account creation and credential checks are delegated to
:class:`backend.app.service.auth.AuthService`; domain exceptions raised there
are mapped to HTTP status codes by the global exception handler.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth.security import create_access_token, get_current_customer
from backend.app.database import get_db
from backend.app.models.user import Customer
from backend.app.schemas.auth import (
    CustomerLoginRequest,
    CustomerRegisterRequest,
    CustomerResponse,
    TokenResponse,
)
from backend.app.service.auth import AuthService

router = APIRouter(prefix="/auth", tags=["Customer Auth"])


@router.post("/register", response_model=TokenResponse)
async def register_customer(payload: CustomerRegisterRequest, db: AsyncSession = Depends(get_db)):
    """Register a new customer account."""
    customer = await AuthService.register_customer(
        name=payload.name,
        email=payload.email,
        password=payload.password,
        phone=payload.phone,
        session=db,
    )
    token = create_access_token(data={"sub": str(customer.id), "type": "customer", "email": customer.email})
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        user={
            "id": customer.id,
            "name": customer.name,
            "email": customer.email,
            "phone": customer.phone,
            "type": "customer",
        },
    )


@router.post("/login", response_model=TokenResponse)
async def login_customer(payload: CustomerLoginRequest, db: AsyncSession = Depends(get_db)):
    """Login customer with email and password."""
    customer = await AuthService.authenticate_customer(
        payload.email, payload.password, session=db
    )
    token = create_access_token(data={"sub": str(customer.id), "type": "customer", "email": customer.email})
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        user={
            "id": customer.id,
            "name": customer.name,
            "email": customer.email,
            "phone": customer.phone,
            "type": "customer",
        },
    )


@router.get("/me", response_model=CustomerResponse)
async def get_me(current_user: Customer = Depends(get_current_customer)):
    """Return currently logged-in customer profile."""
    return current_user
