"""
The records a page produced: describing them, and deleting them safely.

Deleting an invoice does not return its stock on its own - the invoice
bulk-delete actions are a plain .delete() - so an output is taken back here
the way the app reverses stock elsewhere:

  sales invoice     restockOnCancel (sales/utils.py) returns what it deducted
  purchase invoice  take_back_from_stock per line, by its restock ledger
  product           only when nothing else uses it yet
  stock row         deleted if this page created it; otherwise its previous
                    values are put back (BacklogOutput.previous)
"""
from django.db import transaction

from .models import BacklogOutput


class OutputInUse(Exception):
    """The record is referenced elsewhere and cannot simply be deleted."""


def variant_label(variant):
    base = getattr(variant, 'base', None)
    if not base:
        return variant.name or ''
    name = (variant.name or '').strip()
    return f'{base.name} ({name})' if name and name.lower() != 'default' else base.name


def describe(output):
    """What the entry screen shows for one output - only what is on the
    page, never a stock figure or a balance."""
    kind = output.kind
    record = output.record
    row = {'id': output.id, 'kind': kind, 'record_id': getattr(record, 'id', None),
           'created': output.created, 'entered': output.entered}
    if kind == 'sales_invoice':
        row['label'] = f'Sales invoice {record.invoice_number or "#" + str(record.id)}'
        row['detail'] = record.customer.name if record.customer_id else ''
        row['total'] = record.total
    elif kind == 'purchase_invoice':
        row['label'] = f'Purchase invoice {record.invoice_number or "#" + str(record.id)}'
        row['detail'] = record.supplier.name if record.supplier_id else ''
        row['total'] = record.total
    elif kind == 'product':
        row['label'] = record.name
        row['detail'] = record.unit.name if record.unit_id else ''
        row['variant_count'] = record.variants.count()
    elif kind == 'inventory_item':
        row['label'] = variant_label(record.product) if record.product_id else ''
        row['detail'] = record.location.name if record.location_id else ''
    return row


def describe_new_record(row):
    """A customer, supplier or product created from a page, as the owner
    reviewing it sees it - with the opening balance a party came with. Only
    the owner's serializer uses this: the entry person never sees a balance."""
    kind = row.kind
    item = {'id': row.id, 'kind': kind, 'record_id': getattr(row, f'{kind}_id')}
    if kind in ('customer', 'supplier'):
        party = getattr(row, kind)
        opening = getattr(party, 'opening_balance', None)
        item['label'] = party.name
        if kind == 'customer':
            item['detail'] = party.city.name if party.city_id else ''
        else:
            item['detail'] = party.business_name or ''
        item['opening_balance'] = opening.amount if opening else None
    else:
        product = row.product
        count = len(product.variants.all())
        unit = product.unit.name if product.unit_id else ''
        item['label'] = product.name
        item['detail'] = ' · '.join(filter(None, [f'{count} variant{"s" if count != 1 else ""}', unit]))
        item['opening_balance'] = None
    return item


def record_output(entry, record, user, created=True, entered=None, previous=None):
    kind = {
        'SalesInvoice': 'sales_invoice',
        'PurchaseInvoice': 'purchase_invoice',
        'Product': 'product',
        'InventoryItem': 'inventory_item',
    }[type(record).__name__]
    return BacklogOutput.objects.create(
        entry=entry, created=created, created_by=user,
        entered=entered or {}, previous=previous or {},
        **{kind: record})


def _product_in_use(product):
    """Whether any row outside the product's own variants points at it."""
    for variant in product.variants.all():
        for relation in variant._meta.related_objects:
            if relation.one_to_many or relation.one_to_one:
                accessor = relation.get_accessor_name()
                related = getattr(variant, accessor, None)
                if related is None:
                    continue
                if relation.one_to_many and related.exists():
                    return True
    return False


def return_removed_sales_lines(invoice, kept_ids):
    """Before a correction removes lines from a sales invoice, give back what
    those lines took out of stock.

    The invoice update deletes a removed line with its deduction ledger and
    returns nothing - on the invoice screens a line leaves through a return.
    Here a removed line was a typing mistake, so its stock goes back, by the
    ledger, exactly as restockOnCancel does for a whole invoice."""
    from django.db.models import Sum
    from inventory.models import InventoryItem

    for line in invoice.invoice_items.exclude(id__in=kept_ids):
        deducted = line.restocks.aggregate(total=Sum('quantity'))['total'] or 0
        if not deducted:
            continue
        row = InventoryItem.objects.select_for_update().filter(
            business_id=invoice.business_id, product_id=line.product_id).first()
        if row:
            row.quantity += deducted
            row.quantity_on_hand += deducted
            row.save(update_fields=['quantity', 'quantity_on_hand'])
        line.restocks.all().delete()


def delete_output(output):
    """Delete what the output produced, reversing its stock first."""
    from sales.utils import restockOnCancel, take_back_from_stock, units_in_stock

    kind = output.kind
    record = output.record
    with transaction.atomic():
        if kind == 'sales_invoice':
            restockOnCancel(record)
            record.delete()
        elif kind == 'purchase_invoice':
            for line in record.invoice_items.all():
                take_back_from_stock(line, units_in_stock(line))
            record.delete()
        elif kind == 'product':
            if not output.created:
                raise OutputInUse('This product was not created from this page.')
            if _product_in_use(record):
                raise OutputInUse('This product is already used on an invoice or in stock, so it '
                                  'cannot be deleted from here.')
            record.delete()
        elif kind == 'inventory_item':
            if output.created:
                record.delete()
            else:
                for field, value in (output.previous or {}).items():
                    setattr(record, field, value)
                record.save(update_fields=list(output.previous or {}) or None)
                output.delete()
        else:
            output.delete()
