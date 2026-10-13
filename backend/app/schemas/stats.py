"""Business statistics response schemas."""

from datetime import date
from typing import List

from pydantic import BaseModel


class StatsDailyPoint(BaseModel):
    """One UTC day inside the requested range."""

    date: date
    order_count: int
    revenue: float
    cancelled_count: int


class StatsDishSales(BaseModel):
    """One dish's sales inside the requested range."""

    menu_item_id: int
    name: str
    quantity: int
    revenue: float
    order_count: int


class BranchStatsResponse(BaseModel):
    """Order volume, revenue and dish sales for one branch and date range.

    Cancelled orders are excluded from ``order_count`` / ``revenue`` /
    ``units_sold`` / ``top_items`` and reported separately as
    ``cancelled_count``.
    """

    branch_id: int
    branch_name: str
    start_date: date
    end_date: date
    order_count: int
    cancelled_count: int
    revenue: float
    average_order_value: float
    units_sold: int
    top_items: List[StatsDishSales] = []
    daily: List[StatsDailyPoint] = []


__all__ = ["BranchStatsResponse", "StatsDailyPoint", "StatsDishSales"]
