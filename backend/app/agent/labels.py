"""Agent-side re-export of the shared Chinese enum labels.

The canonical mapping lives in :mod:`backend.app.labels` so the Service layer can
render the same labels without importing from ``agent``. Nodes keep importing
from here, which is the Agent-facing name for that vocabulary.
"""

from backend.app.labels import (
    ORDER_STATUS_LABELS,
    ORDER_TYPE_LABELS,
    RESERVATION_STATUS_LABELS,
    SEATING_AREA_LABELS,
    order_type_label,
    seating_area_label,
)

__all__ = [
    "ORDER_TYPE_LABELS",
    "ORDER_STATUS_LABELS",
    "RESERVATION_STATUS_LABELS",
    "SEATING_AREA_LABELS",
    "order_type_label",
    "seating_area_label",
]
