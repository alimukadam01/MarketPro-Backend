"""
Recording one payment against several settleable items.

The party detail screen lets the user enter an amount, tick what it settles,
and have the system work out which payments to create. Two rules shape this
module, both from the BRD and both deliberate:

1. The user chooses every target. The system never picks which invoices get
   paid - it only divides the entered amount across the items the user ticked
   (§6.4).
2. Each payment type settles exactly one kind of thing. On-account money
   settles the opening balance carried over from the paper khaata, and nothing
   else; an invoice is settled only by a payment that names it (§6.6).

So an allocation becomes one transaction per item: a customer_receipt or
supplier_payment for the opening-balance row, and a sale_payment or
purchase_payment for each invoice row. They are written together or not at
all, and they share a payment_group so the rest of the system can treat them
as the single payment they are.
"""

import uuid

from django.db import transaction as db_transaction

from root.models import Customer, Supplier
from sales.models import PurchaseInvoice, SalesInvoice
from sales.utils import invoice_room, live_receipt_total

from .utils import (
    CANCELLED_PURCHASE_STATUSES, CANCELLED_SALES_STATUSES,
    opening_balance_remaining,
)


OPENING_BALANCE = 'opening_balance'
INVOICE = 'invoice'

# Which transaction type each item kind mints, per party. The split is the
# whole point: on-account money and invoice money are summed independently by
# customer_balance/supplier_balance, so writing the wrong one double-counts.
ITEM_TYPES = {
    'customer': {OPENING_BALANCE: 'customer_receipt', INVOICE: 'sale_payment'},
    'supplier': {OPENING_BALANCE: 'supplier_payment', INVOICE: 'purchase_payment'},
}

# The write-only picker field on TransactionCreateSerializer for each party's
# invoices.
INVOICE_FIELDS = {'customer': 'sales_invoice', 'supplier': 'purchase_invoice'}


def _invoice_queryset(business_id, party, party_id):
    if party == 'customer':
        return (
            SalesInvoice.objects
            .filter(business_id=business_id, customer_id=party_id)
            .exclude(status__in=CANCELLED_SALES_STATUSES)
            .prefetch_related('payment_receipts__transaction_record')
            .order_by('date_issued', 'id')
        )

    return (
        PurchaseInvoice.objects
        .filter(business_id=business_id, supplier_id=party_id)
        .exclude(status__in=CANCELLED_PURCHASE_STATUSES)
        .prefetch_related('payment_receipts__transaction_record')
        .order_by('date_issued', 'id')
    )


def settleable_items(business_id, party, party_id):
    """
    Everything this party's money could settle, oldest first.

    The opening balance leads, because it predates every invoice by
    definition. `room` is what the item can still take and is authoritative:
    it comes from invoice_room(), the same helper TransactionCreateSerializer
    validates against, so the dialog can never offer more than the server will
    accept. `reserved` is money already held by a pending cheque - counted
    against room, but settling nothing until it clears.

    party is 'customer' or 'supplier'.
    """
    items = []

    # Two different figures: `owed` is what the party still owes on the
    # balance, `room` is what can still be allocated to it. A pending cheque
    # separates them - it has claimed the money without settling anything -
    # and offering claimed money again would settle the same debt twice.
    _, _, owed = opening_balance_remaining(business_id, party, party_id)
    _, _, room = opening_balance_remaining(
        business_id, party, party_id, include_pending=True)

    if room > 0:
        opening = _opening_row(business_id, party, party_id)
        items.append({
            'kind': OPENING_BALANCE,
            'id': party_id,
            'label': 'Outstanding balance',
            'sub': 'On account · excludes pending invoices',
            'date': opening.as_of_date.isoformat() if opening else None,
            'total': owed,
            'room': room,
            'reserved': max(owed - room, 0),
        })

    for invoice in _invoice_queryset(business_id, party, party_id):
        room = int(invoice_room(invoice))
        if room <= 0:
            continue

        total = float(invoice.total or 0)
        reserved = round(float(live_receipt_total(invoice))
                         - float(invoice.amount_paid or 0))

        items.append({
            'kind': INVOICE,
            'id': invoice.id,
            'label': invoice.invoice_number or f'#{invoice.id}',
            'sub': None,
            'date': invoice.date_issued.date().isoformat()
            if invoice.date_issued else None,
            'total': round(total),
            'room': room,
            'reserved': max(reserved, 0),
        })

    return items


def _opening_row(business_id, party, party_id):
    from .models import PartyOpeningBalance
    return (
        PartyOpeningBalance.objects
        .filter(business_id=business_id, **{f'{party}_id': party_id})
        .first()
    )


def allocate(items, amount, order='oldest'):
    """
    Split `amount` across `items` in the chosen order, filling each one up to
    its room before moving on. Pure; touches no database.

    Returns [(item, applied)] for the items that received something, in the
    order the money reached them.

    Whole rupees only: Transaction.amount and PartyOpeningBalance.amount are
    IntegerFields while invoice_room() returns a float. A fractional residue
    would leave the dialog's "fully allocated" gate permanently unsatisfiable,
    so every split is an int and the last reached item absorbs the remainder.
    """
    ordered = list(reversed(items)) if order == 'newest' else list(items)

    left = int(amount)
    applied_rows = []

    for item in ordered:
        if left <= 0:
            break

        room = int(item['room'])
        if room <= 0:
            continue

        applied = min(left, room)
        left -= applied
        applied_rows.append((item, applied))

    return applied_rows, left


def record_party_payment(business_id, user_id, party, party_id, amount,
                         item_ids, money_details, context, order='oldest'):
    """
    Write one payment as one transaction per settled item, atomically.

    Delegates every write to TransactionCreateSerializer, which already owns
    the type/source rules, the invoice-room check and the delegation to
    SalesReceipt / PurchaseReceipt / PartyPayment. This function owns only the
    split, the group id and the all-or-nothing boundary.

    `item_ids` is the list of {kind, id} the user ticked. Returns the created
    transactions in allocation order.
    """
    from .serializers import TransactionCreateSerializer

    available = {(item['kind'], item['id']): item
                 for item in settleable_items(business_id, party, party_id)}

    chosen = []
    for ref in item_ids:
        key = (ref['kind'], int(ref['id']))
        item = available.get(key)
        if item is not None:
            chosen.append(item)

    applied_rows, _ = allocate(chosen, amount, order)

    group = uuid.uuid4()
    types = ITEM_TYPES[party]
    invoice_field = INVOICE_FIELDS[party]
    created = []

    with db_transaction.atomic():
        for item, applied in applied_rows:
            payload = dict(money_details)
            payload['amount'] = applied
            payload['type'] = types[item['kind']]

            if item['kind'] == OPENING_BALANCE:
                payload[party] = party_id
            else:
                payload[invoice_field] = item['id']

            serializer = TransactionCreateSerializer(
                data=payload, context=context)
            serializer.is_valid(raise_exception=True)
            transaction = serializer.save()

            # The group is stamped after the fact because the delegated
            # branches mint their transaction through a signal, so this is the
            # first point at which the row exists.
            transaction.payment_group = group
            transaction.save(update_fields=['payment_group'])
            created.append(transaction)

    return created


def resolve_party(business_id, customer_id=None, supplier_id=None):
    """
    (party, party_id) for a request naming exactly one of the two, or
    (None, None) when neither names a party in this business.
    """
    if customer_id:
        exists = Customer.objects.filter(
            id=customer_id, business_id=business_id).exists()
        return ('customer', int(customer_id)) if exists else (None, None)

    if supplier_id:
        exists = Supplier.objects.filter(
            id=supplier_id, business_id=business_id).exists()
        return ('supplier', int(supplier_id)) if exists else (None, None)

    return None, None
