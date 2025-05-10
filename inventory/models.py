from django.db import models
from django.conf import settings
from root.models import Business, BaseItem, Location

# Create your models here.

class Inventory(models.Model):
    business = models.OneToOneField(Business, models.CASCADE, related_name='inventory_glance')
    total_quantity_on_hand = models.IntegerField(null=True, blank=True)
    total_value_reserved = models.IntegerField(null=True, blank=True)
    net_inventory_value = models.FloatField(null=True, blank=True)
    last_transaction = models.IntegerField(null=True, blank=True)
    last_transaction_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.business}"


class InventoryItem(BaseItem):
    inventory = models.ForeignKey(Inventory, models.CASCADE, related_name='items')
    location = models.ForeignKey(Location, models.CASCADE, related_name='inventory_items')
    quantity_on_hand = models.IntegerField(null=True, blank=True)
    quantity_reserved = models.IntegerField(null=True, blank=True)
    unit_cost = models.FloatField(null=True, blank=True)
    reorder_level = models.IntegerField(null=True, blank=True)
    last_transaction_id = models.IntegerField(null=True, blank=True)
    last_transaction_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.quantity} x {self.product.name}"
    
    class Meta:
        unique_together = [('inventory', 'product')]

class InventoryItemHistory(InventoryItem):
    version_no = models.IntegerField()
    item = models.ForeignKey(InventoryItem, models.CASCADE, related_name='history')
    effective_at = models.DateTimeField(auto_now=True)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, models.CASCADE)
    change_reason = models.TextField(null=True, blank=True)


