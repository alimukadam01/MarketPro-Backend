from django.db.models.signals import pre_save, post_save, post_delete
from django.dispatch import receiver

from root.utils import generateTransactionId
from .models import PurchaseInvoice, PurchaseInvoiceItem, SalesInvoice, SalesInvoiceItem, ReturnedItem
from .utils import (
    restock_purchase,
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


### automatically update inventory on purchase
@receiver(post_save, sender=PurchaseInvoice)
def updateInventoryOnPurchase(sender, instance: PurchaseInvoice, **kwargs):
    # Moved to sales/utils.py, which reads the lines fresh and cannot re-enter
    # itself - see restock_purchase.
    restock_purchase(instance)


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
