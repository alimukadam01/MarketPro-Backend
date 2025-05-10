from django.contrib import admin
from .models import Inventory, InventoryItem

admin.site.register(Inventory)
admin.site.register(InventoryItem)

