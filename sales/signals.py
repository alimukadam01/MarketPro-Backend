from django.db.models.signals import pre_save, post_save, post_delete
from django.dispatch import receiver

from root.utils import generateTransactionId
from .models import PurchaseInvoice, PurchaseInvoiceItem, SalesInvoice, SalesInvoiceItem, ReturnedItem
from .utils import (
    getRestockField, update_inventory, spiltNewAndOldProducts, 
    logRestockEvent, createInventoryItemFromRestock
)


### update invoice totals
@receiver(post_save, sender=SalesInvoiceItem)
@receiver(post_delete, sender=SalesInvoiceItem)
def updateTotalsAfterSalesInvoiceItem(sender, instance, **kwargs):
    if instance.sales_invoice:
        instance.sales_invoice.adjust_totals()


### automatically update inventory on purchase
@receiver(post_save, sender=PurchaseInvoice)
def updateInventoryOnPurchase(sender, instance: PurchaseInvoice, **kwargs):
    if instance.is_restocked: return

    if instance.status in ('R', 'PR'):
        
        if instance.status == 'R' and not instance.is_restocked:
            quantity_field = getRestockField(False)
        else:
            quantity_field = getRestockField(True)

        product_item_mapping = instance.map_to_products()
        product_ids = set(product_item_mapping)
        existing, _, new_ids = spiltNewAndOldProducts(product_ids, instance.business.id)

        for inventory_item in existing:
            item = product_item_mapping[inventory_item.product_id]
            morphed_item = item.morph()
            delta = item.compute_restock_delta()
            if delta > 0:
                inventory_item.apply_restock_delta(False, delta, morphed_item['last_transaction'])
                logRestockEvent(item, delta, instance)
                item.update_restock_flags()

        for pid in new_ids:
            item = product_item_mapping[pid]
            inventory_item = createInventoryItemFromRestock(item, quantity_field)
            logRestockEvent(item, getattr(item, quantity_field), instance)
            item.update_restock_flags()

        instance.update_restock_flags()


### update invoice totals
@receiver(post_save, sender=PurchaseInvoiceItem)
@receiver(post_delete, sender=PurchaseInvoiceItem)
def updateTotalsAfterPurchaseInvoiceItem(sender, instance, **kwargs):
    if instance.purchase_invoice:
        instance.purchase_invoice.adjust_totals()

### Create delete signal
### automatically update inventory on sale
@receiver(post_save, sender=SalesInvoice)
def updateInventoryOnSale(sender, instance: SalesInvoice, **kwargs):
    if instance.is_deducted: return

    if instance.status in ('C', 'PC'):

        if instance.status == 'C' and not instance.is_deducted:
            quantity_field = getRestockField(False)
        else:
            quantity_field = getRestockField(True)

        invoice_items_dict = instance.map_to_products()
        product_ids = set(invoice_items_dict)
        existing, _, new_ids = spiltNewAndOldProducts(product_ids, instance.business.id)

        for inventory_item in existing:
            item = invoice_items_dict[inventory_item.product_id]
            delta = item.compute_restock_delta()
            if delta > 0:
                inventory_item.apply_restock_delta(True, delta, generateTransactionId(instance))
                logRestockEvent(item, delta, instance)
                item.update_restock_flags()

        ### Fix logic here
        instance.update_deduction_flags()


@receiver(post_save, sender=ReturnedItem)
def mark_item_as_returned(sender, instance: ReturnedItem, created, **kwargs):
    if created:
        invoice_item = instance.invoice_item
        
        '''
        if instance.quantity < instance.invoice_item.quantity:
            invoice_item.is_partially_returned = True
            invoice_item.save(update_fields=['is_partially_returned'])
            return
        '''
        
        invoice_item.is_returned = True
        invoice_item.save(update_fields=['is_returned'])


@receiver(post_delete, sender=ReturnedItem)
def unmark_item_as_returned(sender, instance, **kwargs):
    instance.invoice_item.is_returned = False
    instance.invoice_item.save(update_fields=['is_returned'])