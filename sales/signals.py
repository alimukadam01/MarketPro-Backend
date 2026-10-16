from django.db.models.signals import pre_save, post_save, post_delete
from django.dispatch import receiver

from root.utils import generateTransactionId
from .models import PurchaseInvoice, PurchaseInvoiceItem, SalesInvoice, SalesInvoiceItem, ReturnedItem
from .utils import (
    getRestockField, update_inventory, spiltNewAndOldProducts, 
    logRestockEvent, createInventoryItemFromRestock,
    deleting_with_invoice, sync_charge_expenses,
)


### update invoice totals
@receiver(post_save, sender=SalesInvoiceItem)
@receiver(post_delete, sender=SalesInvoiceItem)
def updateTotalsAfterSalesInvoiceItem(sender, instance, **kwargs):
    if instance.sales_invoice:
        instance.sales_invoice.adjust_totals()
        # Gaining or losing a line is what moves an invoice between Draft and
        # Completed, so the status is re-derived here too. Safe to sit on the
        # item signal: recalculate_status writes with a queryset update, which
        # fires nothing, so it cannot re-enter adjust_totals above.
        instance.sales_invoice.recalculate_status()
        # Draft and Completed differ in whether the charges are expensed, so a
        # first or a last line changes them too. Not while the whole invoice
        # is being deleted - see deleting_with_invoice.
        if not deleting_with_invoice(kwargs.get('origin')):
            sync_charge_expenses(instance.sales_invoice)


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
                inventory_item.apply_restock_delta(False, delta, morphed_item['last_transaction'], morphed_item['unit_cost'])
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

# There was a post_save receiver on SalesInvoice here, a second copy of
# updateInventoryOnSale. It was deleted rather than fixed, for three reasons:
#
#   - Its writes were discarded. It mutated the instance and never saved, so
#     only the copy in sales/utils.py (called explicitly by the two bundled
#     serializers) ever reached the database. Two implementations of one rule,
#     one of them silently inert, is how the status bug stayed hidden.
#   - It fired from inside adjust_totals(), which saves the invoice from within
#     the item post_save below - an invoice -> item -> invoice cycle that only
#     terminated because the deltas eventually reached zero.
#   - It could not see a final item set anyway: the update serializer saves the
#     invoice before creating and deleting lines, so a receiver at that moment
#     reads the invoice as it was, not as it will be.
#
# sales/views.py:capture-customer used .update() purely to dodge it; that is
# now simply the right write for a one-column change.


@receiver(post_save, sender=ReturnedItem)
def updateTotalsAfterReturnItem(sender, instance, created, **kwargs):
    instance.invoice_item.sales_invoice.adjust_totals()
