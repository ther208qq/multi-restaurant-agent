"""Public Branch and Menu Explorer Endpoints."""

from typing import List

from fastapi import APIRouter

from backend.app.schemas.branch import BranchResponse
from backend.app.service.branch import BranchService
from backend.app.service.faq import FAQService
from backend.app.service.menu import MenuService

router = APIRouter(prefix="/branches", tags=["Branches & Menus"])


@router.get("", response_model=List[BranchResponse])
async def list_active_branches():
    """List all active restaurant branches."""
    return await BranchService.list_active_branches()


@router.get("/{branch_id}/menu")
async def get_branch_menu(branch_id: int):
    """Retrieve full menu for a branch including real-time stock and availability."""
    return await MenuService.get_branch_menu(branch_id)


@router.get("/{branch_id}/faq")
async def get_branch_info(branch_id: int):
    """Retrieve opening hours, address, contact, and dining policies for a branch."""
    return await FAQService.get_branch_info(branch_id)
