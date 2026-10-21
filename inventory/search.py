"""What the inventory app makes searchable: stock items."""
from root.search import Hit, SearchType, link, register, status
from root.search.labels import joined, product_label

from .models import InventoryItem


def _stock_hit(item):
    low = item.reorder_level is not None and (item.quantity_on_hand or 0) <= item.reorder_level
    return Hit(
        id=item.id,
        title=product_label(item.product),
        subtitle=joined(getattr(item.location, "name", ""), f"{item.quantity_on_hand or 0} on hand"),
        amount=item.unit_price,
        status=status("LOW", "Below reorder level", "warning") if low else None,
        link=link("/inventory/update-item", item_id=item.id),
    )


register(SearchType(
    key="inventory_item",
    label="Stock items",
    model=InventoryItem,
    module="inventory",
    text_fields=["product__name", "product__base__name", "product__sku",
                 "location__name", "track_code", "notes"],
    number_fields=["id"],
    select_related=["product__base", "location"],
    order_by=["product__base__name", "product__name", "id"],
    priority=110,
    to_hit=_stock_hit,
))
