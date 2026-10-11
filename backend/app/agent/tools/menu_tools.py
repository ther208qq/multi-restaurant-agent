"""Menu Agent Tool — thin adapter over :class:`MenuService`.

Holds no business rules and no direct data access. It only adapts the Service
result to the dict shape the Agent nodes consume, and converts domain
exceptions into the ``{"error": ...}`` payload the nodes already understand.
"""

from typing import Any, Dict

from backend.app.exceptions import DomainError
from backend.app.service.menu import MenuService


async def get_branch_menu_data(branch_id: int) -> Dict[str, Any]:
    """Full active menu for a branch, including stock levels and variants.

    Delegates to ``MenuService.get_branch_menu`` (the single implementation
    shared with the public menu API).
    """
    try:
        return await MenuService.get_branch_menu(branch_id)
    except DomainError as exc:
        return {"error": exc.message}


async def format_menu_for_prompt(branch_id: int) -> str:
    """Format the branch menu into a clean text summary for LLM context."""
    data = await get_branch_menu_data(branch_id)
    if "error" in data:
        return data["error"]

    lines = [f"--- Menu for {data['branch_name']} ---"]
    for cat in data["categories"]:
        # A category with nothing in it is junk on a customer-facing menu -- a
        # leftover test row, say. Printing a bare "[Name]" header helps nobody.
        if not cat["items"]:
            continue
        lines.append(f"\n[{cat['name']}]")
        for item in cat["items"]:
            if not item["effective_available"]:
                lines.append(f"  • {item['name']} - [OUT OF STOCK / UNAVAILABLE]")
                continue

            variant_desc = []
            for v in item["variants"]:
                delta_str = f" (+${v['price_delta']:.2f})" if v["price_delta"] > 0 else (f" (-${abs(v['price_delta']):.2f})" if v["price_delta"] < 0 else "")
                variant_desc.append(f"{v['name']}{delta_str}")

            var_info = f" (Options: {', '.join(variant_desc)})" if variant_desc else ""
            stock_info = f" [Stock: {item['stock_quantity']}]"
            lines.append(f"  • #{item['id']} {item['name']} - ${item['base_price']:.2f}{var_info}{stock_info}")
            if item["description"]:
                lines.append(f"    {item['description']}")

    return "\n".join(lines)
