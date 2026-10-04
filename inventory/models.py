from django.db import models
from django.conf import settings
from root.utils import generateTransactionId
from django.db.models.functions import Coalesce
from root.models import BaseQuerySet, Business, BaseItem, Location, ProductVariant
# from sales.utils import printObject

# Create your models here.
class InventoryQuerySet(BaseQuerySet):

    def get_items(self, business_id):
        """
        Every item in one business's inventory.

        Looked up by business rather than by pk. Every caller passes a business
        id and always has, but this read `self.get(id=id)`, which only agreed
        with them because inventories happen to have been created in the same
        order as their businesses - the signal makes one per Business, so the
        two id sequences run in step until anything is deleted. Once they
        diverge, a business reports another business's stock.

        `.filter().first()` rather than `.get()` so a business whose inventory
        row does not exist yet reports zero instead of raising.
        """
        inventory = self.filter(business_id=business_id).first()
        return inventory.items.all() if inventory else InventoryItem.objects.none()


class InventoryManager(models.Manager):

    def get_queryset(self):
        return InventoryQuerySet(self.model)
    
    def total_inventory_value(self, business_id):
        """What the stock on hand cost to buy: quantity times unit_cost."""
        return self._stock_value(business_id, 'unit_cost')

    def total_inventory_value_with_profit(self, business_id):
        """
        What the same stock sells for: quantity times unit_price. The gap
        between this and total_inventory_value is the profit sitting in the
        inventory.
        """
        return self._stock_value(business_id, 'unit_price')

    def _stock_value(self, business_id, price_field):
        # Aggregated in SQL rather than summed in Python, which is how
        # total_inventory_value read before. unit_cost and unit_price are both
        # nullable and `quantity * None` raises TypeError - the fault that used
        # to take average_order_value down. Coalesce says what a missing price
        # is worth here, nothing, instead of letting SQL drop the row silently.
        items = self.get_queryset().get_items(business_id)
        total = items.aggregate(
            total=models.Sum(
                models.F('quantity') * Coalesce(models.F(price_field), 0.0),
                output_field=models.FloatField(),
            )
        )['total']

        return total or 0

    def total_restocks_required(self, business_id):
        items = self.get_queryset().get_items(business_id)
        return items.filter(quantity_on_hand__lte = models.F('reorder_level')).count()

    def total_items_not_in_inventory(self, business_id):
        """
        Variants the business sells that have no inventory row at all - not a
        quantity of zero, no record. InventoryItem is unique per
        (inventory, product), so a missing row is a missing variant.

        Inactive variants, and variants of inactive products, are left out:
        the number reads as work still to do, and a discontinued line is not.
        """
        stocked = self.get_queryset().get_items(business_id).values('product_id')

        return (
            ProductVariant.objects
            .filter(base__business_id=business_id, is_active=True, base__is_active=True)
            .exclude(id__in=stocked)
            .count()
        )


class Inventory(models.Model):
    business = models.OneToOneField(Business, models.CASCADE, related_name='inventory_glance')
    total_quantity_on_hand = models.IntegerField(null=True, blank=True)
    total_value_reserved = models.IntegerField(null=True, blank=True)
    net_inventory_value = models.FloatField(null=True, blank=True)
    last_transaction = models.CharField(max_length=256, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = InventoryManager()

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
        return f"{self.quantity} x {self.product.base.name} ({self.product.name})"
    
    def apply_restock_delta(self, is_sold, delta: int, last_transaction: str, amount=None):
        """
        Applies a restock delta to this inventory row.
        """
        if not is_sold:
            self.quantity += delta
            self.quantity_on_hand += delta
            self.unit_cost = amount
            self.last_transaction = last_transaction
            self.save(update_fields=['quantity', 'quantity_on_hand', 'unit_cost', 'last_transaction'])
    
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
