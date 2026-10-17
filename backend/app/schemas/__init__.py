"""Schemas Registry."""

from backend.app.schemas.auth import (
    CustomerLoginRequest,
    CustomerRegisterRequest,
    CustomerResponse,
    StaffLoginRequest,
    StaffResponse,
    TokenResponse,
)
from backend.app.schemas.audit import AuditLogResponse
from backend.app.schemas.branch import (
    BranchCreate,
    BranchResponse,
    BranchUpdate,
    TableCreate,
    TableResponse,
    TableUpdate,
)
from backend.app.schemas.menu import (
    MenuCategoryCreate,
    MenuCategoryResponse,
    MenuItemCreate,
    MenuItemResponse,
    MenuItemStockResponse,
    MenuItemStockUpdate,
    MenuItemUpdate,
    MenuItemVariantCreate,
    MenuItemVariantResponse,
)
from backend.app.schemas.order import (
    OrderCreate,
    OrderItemCreate,
    OrderItemResponse,
    OrderPreviewLine,
    OrderPreviewResponse,
    OrderResponse,
    OrderStatusUpdate,
)
from backend.app.schemas.reservation import (
    ReservationAvailabilityItem,
    ReservationCreate,
    ReservationResponse,
    ReservationUpdate,
    parse_slot,
)
from backend.app.schemas.stats import (
    BranchStatsResponse,
    StatsDailyPoint,
    StatsDishSales,
)

__all__ = [
    "CustomerLoginRequest",
    "CustomerRegisterRequest",
    "CustomerResponse",
    "StaffLoginRequest",
    "StaffResponse",
    "TokenResponse",
    "AuditLogResponse",
    "BranchCreate",
    "BranchResponse",
    "BranchUpdate",
    "TableCreate",
    "TableResponse",
    "TableUpdate",
    "MenuCategoryCreate",
    "MenuCategoryResponse",
    "MenuItemCreate",
    "MenuItemResponse",
    "MenuItemStockResponse",
    "MenuItemStockUpdate",
    "MenuItemUpdate",
    "MenuItemVariantCreate",
    "MenuItemVariantResponse",
    "OrderCreate",
    "OrderItemCreate",
    "OrderItemResponse",
    "OrderPreviewLine",
    "OrderPreviewResponse",
    "OrderResponse",
    "OrderStatusUpdate",
    "ReservationAvailabilityItem",
    "ReservationCreate",
    "ReservationResponse",
    "ReservationUpdate",
    "parse_slot",
    "BranchStatsResponse",
    "StatsDailyPoint",
    "StatsDishSales",
]
