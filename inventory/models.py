from django.db import models
from django.conf import settings
from root.utils import generateTransactionId
from root.models import Business, BaseItem, Location
# from sales.utils import printObject

# Create your models here.

class Inventory(models.Model):
    business = models.OneToOneField(Business, models.CASCADE, related_name='inventory_glance')
    total_quantity_on_hand = models.IntegerField(null=True, blank=True)
    total_value_reserved = models.IntegerField(null=True, blank=True)
    net_inventory_value = models.FloatField(null=True, blank=True)
    last_transaction = models.CharField(max_length=256, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.business}"
    
    
class InventoryItem(BaseItem):
    inventory = models.ForeignKey(Inventory, models.CASCADE, related_name='items')
    location = models.ForeignKey(
        Location, models.CASCADE, 
        related_name='inventory_items', 
        null=True, blank=True
    )
    quantity_on_hand = models.IntegerField(default=0)
    quantity_reserved = models.IntegerField(default=0)
    unit_cost = models.FloatField(null=True, blank=True)
    unit_price = models.FloatField(null=True, blank=True)
    reorder_level = models.IntegerField(null=True, blank=True)
    last_transaction = models.CharField(max_length=256, null=True, blank=True)

    def __str__(self):
        return f"{self.quantity} x {self.product.name}"
    
    def apply_restock_delta(self, is_sold, delta: int, last_transaction: str ):
        """
        Applies a restock delta to this inventory row.
        """
        if not is_sold:
            self.quantity += delta
            self.quantity_on_hand += delta
        else:
            self.quantity -= delta
            self.quantity_on_hand -= delta

        self.last_transaction = last_transaction
        self.save(update_fields=['quantity', 'quantity_on_hand', 'last_transaction'])
    
    class Meta:
        unique_together = [('inventory', 'product')]

class InventoryItemHistory(InventoryItem):
    version_no = models.IntegerField()
    item = models.ForeignKey(InventoryItem, models.CASCADE, related_name='history')
    effective_at = models.DateTimeField(auto_now=True)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, models.CASCADE)
    change_reason = models.TextField(null=True, blank=True)
