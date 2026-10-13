"""Staff Branch and Table Management Routes."""

from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth.security import get_current_staff, require_admin
from backend.app.database import get_db
from backend.app.models.user import StaffUser
from backend.app.schemas.branch import (
    BranchCreate,
    BranchResponse,
    BranchUpdate,
    TableCreate,
    TableResponse,
    TableUpdate,
)
from backend.app.service.branch import BranchService

router = APIRouter(prefix="/staff/branches", tags=["Staff Branches & Tables"])


@router.get("", response_model=List[BranchResponse])
async def list_branches(
    current_staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
):
    """List branches with their tables."""
    return await BranchService.list_branches(current_staff, session=db)


@router.post("", response_model=BranchResponse)
async def create_branch(
    payload: BranchCreate,
    current_admin: StaffUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create a new restaurant branch (Admin only)."""
    return await BranchService.create_branch(
        payload.model_dump(), actor=current_admin, session=db
    )


@router.patch("/{branch_id}", response_model=BranchResponse)
async def update_branch(
    branch_id: int,
    payload: BranchUpdate,
    current_admin: StaffUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update branch details (Admin only)."""
    return await BranchService.update_branch(
        branch_id, payload.model_dump(exclude_unset=True), actor=current_admin, session=db
    )


# Table Management
@router.get("/tables", response_model=List[TableResponse])
async def list_tables(
    branch_id: Optional[int] = Query(None),
    current_staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
):
    """List tables for a branch."""
    return await BranchService.list_tables(current_staff, branch_id, session=db)


@router.post("/tables", response_model=TableResponse)
async def create_table(
    payload: TableCreate,
    current_staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
):
    """Add a new table to a branch."""
    return await BranchService.create_table(
        current_staff, payload.model_dump(), session=db
    )


@router.patch("/tables/{table_id}", response_model=TableResponse)
async def update_table(
    table_id: int,
    payload: TableUpdate,
    current_staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
):
    """Update table label, seating area, capacity, or availability."""
    return await BranchService.update_table(
        current_staff, table_id, payload.model_dump(exclude_unset=True), session=db
    )
