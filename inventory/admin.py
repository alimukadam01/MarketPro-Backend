from django.contrib import admin

from core.admin_masking import MaskedModelAdmin

from .models import Inventory, InventoryItem, InventoryItemHistory

# Registered through MaskedModelAdmin so currency amounts render as asterisks.
# InventoryItem is the sharpest case: unit_cost and unit_price sit next to each
# other on one form, so an unmasked pair gives away margin per SKU at a glance.
for model in (Inventory, InventoryItem):
    admin.site.register(model, MaskedModelAdmin)