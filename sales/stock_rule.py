"""
The stock count cut-off (§8).

A person who counts stock and types the figure in (InventoryItem.
stock_counted_at) has counted everything sold or bought up to that day. So a
sale or purchase line dated on or before the count must not move that stock
again - which is exactly what backfilling years of paper invoices would
otherwise do.

  sales line     dated by its invoice's date_issued
  purchase line  by the delivery date, else the invoice's date_issued
  return         by the day it was recorded (a ReturnedItem has no date)

A line held back by the count is marked done (is_deducted / is_restocked)
with `stock_skipped` set and nothing in its ledger, so the existing guards
pass over it and a later recount cannot process it twice.

When an invoice's date moves across the count, reconcile_*_invoice puts it
right: a skipped line now after the count is unmarked so the normal path
moves its stock; a line that had moved stock and is now on or before the
count has exactly its ledger taken back.
"""
from django.db.models import Sum

from inventory.models import InventoryItem
from root.utils import local_date


def counted_at(business_id, product_id):
    return (InventoryItem.objects
            .filter(business_id=business_id, product_id=product_id)
            .values_list('stock_counted_at', flat=True).first())


def line_date(line):
    if getattr(line, 'sales_invoice_id', None):
        issued = line.sales_invoice.date_issued
        return local_date(issued) if issued else None
    invoice = line.purchase_invoice
    if invoice.delivery:
        return invoice.delivery
    return local_date(invoice.date_issued) if invoice.date_issued else None


def stock_dates(invoice):
    """The dates an invoice's lines are judged by. The reconcile runs only
    when these change: a recount made later already includes the lines
    processed before it, and must not make an unrelated edit move them."""
    issued = local_date(invoice.date_issued) if invoice.date_issued else None
    return (issued, getattr(invoice, 'delivery', None))


def stock_applies(line):
    """Whether this line should move stock: no count yet, no date to judge
    it by, or dated after the count."""
    counted = counted_at(line.business_id, line.product_id)
    day = line_date(line)
    return counted is None or day is None or day > counted


def return_applies(returned):
    """Whether a return to inventory should add stock: after the count of its
    product, judged by the day it was recorded."""
    line = returned.invoice_item
    counted = counted_at(line.business_id, line.product_id)
    return counted is None or local_date(returned.created_at) > counted


def skip_sales_line(line):
    line.stock_skipped = True
    line.is_deducted = True
    line.save(update_fields=['stock_skipped', 'is_deducted'])


def skip_purchase_line(line):
    line.stock_skipped = True
    line.is_restocked = True
    line.is_partially_restocked = False
    line.save(update_fields=['stock_skipped', 'is_restocked', 'is_partially_restocked'])


def _return_sales_ledger(line):
    """Put back what a sales line took out of stock, and clear its ledger -
    restockOnCancel's per-line step."""
    from root.utils import generateTransactionId
    deducted = line.restocks.aggregate(total=Sum('quantity'))['total'] or 0
    if not deducted:
        return
    row = InventoryItem.objects.filter(business_id=line.business_id, product_id=line.product_id).first()
    if row:
        row.quantity += deducted
        row.quantity_on_hand += deducted
        row.last_transaction = generateTransactionId(line.sales_invoice)
        row.save(update_fields=['quantity', 'quantity_on_hand', 'last_transaction'])
    line.restocks.all().delete()


def reconcile_sales_invoice(invoice):
    """Bring a sales invoice's lines in line with the counts after its date
    changed. Does not deduct: updateInventoryOnSale, run after it, takes out
    whatever now applies."""
    for line in invoice.invoice_items.all():
        applies = stock_applies(line)
        if line.stock_skipped and applies:
            line.stock_skipped = False
            line.is_deducted = False
            line.save(update_fields=['stock_skipped', 'is_deducted'])
        elif not line.stock_skipped and not applies:
            _return_sales_ledger(line)
            skip_sales_line(line)


def reconcile_purchase_invoice(invoice):
    """The purchase counterpart. Returns True when a line needs stocking
    again, so the caller runs restock_purchase."""
    from sales.utils import take_back_from_stock, units_in_stock
    needs_restock = False
    for line in invoice.invoice_items.all():
        applies = stock_applies(line)
        if line.stock_skipped and applies:
            line.stock_skipped = False
            line.is_restocked = False
            line.save(update_fields=['stock_skipped', 'is_restocked'])
            needs_restock = True
        elif not line.stock_skipped and not applies:
            take_back_from_stock(line, units_in_stock(line))
            skip_purchase_line(line)
    if needs_restock and invoice.is_restocked:
        invoice.is_restocked = False
        invoice.save(update_fields=['is_restocked'])
    return needs_restock
