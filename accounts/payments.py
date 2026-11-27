"""
Recording one payment from the party screen.

The user enters an amount and may tick invoices. Two rules shape this module:

1. Ticking is optional. A ticked invoice is settled by a payment that names
   it: a sale_payment or purchase_payment, filled up to its room in the order
   the user chose.
2. Whatever is not applied to a ticked invoice - all of it, when nothing is
   ticked - goes on account as one customer_receipt or supplier_payment. The
   khaata settles that the way it settles all unnamed money
   (accounts/settlement.py): the opening balance first, then the oldest
   invoices. Anything beyond what the party owes stays on the khaata as
   credit and settles their next invoice.

The opening balance is therefore never ticked: on-account money reaches it
first anyway.

The sides are the person's (register #25): Payable is everything the
business owes them, which can include a customer's advance, and Receivable
everything they owe, which can include money a supplier holds. So before
going on account, the payment first pays back that other khaata's advance,
as a refund on it: 'You pay' refunds a customer's advance, '<name> pays you'
collects what a supplier holds. A person without the role for the payment's
own side can still be paid back an advance this way, up to the advance.

So one payment becomes one transaction per ticked invoice, at most one refund
of the other side's advance, and at most one on-account transaction. They are written together or not at all, named
payments first - each is checked against its invoice's room, which the
on-account part would otherwise shrink - and they share a payment_group so the
rest of the system can treat them as the single payment they are.
"""

import uuid

from django.db import transaction as db_transaction

from rest_framework import serializers

from root.models import Customer, Supplier
from sales.models import PurchaseInvoice, SalesInvoice
from sales.utils import invoice_room, live_receipt_total

from .models import Transaction
from .settlement import LABELS, attach_settlement, settle_parties, settle_party
from .utils import (
    NOT_A_DEBT_PURCHASE_STATUSES, NOT_A_DEBT_SALES_STATUSES, party_effect_totals,
)


ON_ACCOUNT = 'on_account'
INVOICE = 'invoice'
# A loan on this side of the person's khaata (register #21, R18). A ticked
# loan is repaid by a repayment that names it (PartyPayment.loan).
LOAN = 'loan'
# The loan each side can carry: one the business gave (the person owes it
# back) or one it received (the business owes it back).
LOAN_TYPE = {'customer': 'loan_given', 'supplier': 'loan_received'}

# Which transaction type each part of a payment mints, per party. The split is
# the whole point: on-account money and invoice money are summed independently
# by customer_balance/supplier_balance, so writing the wrong one double-counts.
ITEM_TYPES = {
    'customer': {ON_ACCOUNT: 'customer_receipt', INVOICE: 'sale_payment'},
    'supplier': {ON_ACCOUNT: 'supplier_payment', INVOICE: 'purchase_payment'},
}

# The write-only picker field on TransactionCreateSerializer for each party's
# invoices.
INVOICE_FIELDS = {'customer': 'sales_invoice', 'supplier': 'purchase_invoice'}

# The other khaata's advance a payment pays back first, and the refund that
# does it: paying a person ('supplier' side) refunds their customer advance;
# being paid ('customer' side) collects what their supplier role holds.
ADVANCE = 'advance'
ADVANCE_REFUND = {
    'customer': ('supplier', 'purchase_return_refund'),
    'supplier': ('customer', 'sales_return_refund'),
}


def other_side_advance(business_id, party, person):
    """
    (role_id, room): the person's other role and how far its khaata runs past
    zero - the advance this payment can pay back - less any refund of it
    already waiting in a pending cheque. (None, 0) when there is none.
    """
    from root.people import role_of

    other_kind, _ = ADVANCE_REFUND[party]
    role = role_of(person, other_kind) if person is not None else None
    if role is None:
        return None, 0
    advance = max(-round(settle_party(business_id, other_kind, role.id).balance), 0)
    if advance:
        _, pending = party_effect_totals(
            business_id, {f'person__{other_kind}__id': role.id}, other_kind,
            statuses=('PEN',), money_only=True)
        advance = max(advance - round(pending), 0)
    return role.id, advance


def _invoice_queryset(business_id, party, party_id):
    if party == 'customer':
        return (
            SalesInvoice.objects
            .filter(business_id=business_id, customer_id=party_id)
            .exclude(status__in=NOT_A_DEBT_SALES_STATUSES)
            .prefetch_related('payment_receipts__transaction_record')
            .order_by('date_issued', 'id')
        )

    return (
        PurchaseInvoice.objects
        .filter(business_id=business_id, supplier_id=party_id)
        .exclude(status__in=NOT_A_DEBT_PURCHASE_STATUSES)
        .prefetch_related('payment_receipts__transaction_record')
        .order_by('date_issued', 'id')
    )


def settleable_items(business_id, party, party_id):
    """
    The invoices this party's money could be named against, oldest first.

    The opening balance is not listed: on-account money settles it first
    anyway (see the module docstring), and the dialog reads what is left on it
    from the endpoint's opening_balance block.

    `room` is what the invoice can still take and is authoritative: it comes
    from invoice_room(), the same helper TransactionCreateSerializer validates
    against, so the dialog can never offer more than the server will accept.
    It ignores the khaata: an invoice takes its own payments first, so one the
    advance covers is still listed, with `covered` saying how much the khaata
    settles on it. Paying it returns that much to the khaata. `owed` is what
    is still unpaid once the khaata has settled. `reserved` is what its own
    pending cheques hold, counted against room but settling nothing yet.

    party is 'customer' or 'supplier'.
    """
    items = []

    invoices = list(_invoice_queryset(business_id, party, party_id))
    attach_settlement(invoices)
    for invoice in invoices:
        room = int(invoice_room(invoice))
        if room <= 0:
            continue

        total = float(invoice.total or 0)
        owed = max(total - float(invoice.amount_paid or 0), 0)
        covered = float(invoice.pool_applied or 0)

        items.append({
            'kind': INVOICE,
            'id': invoice.id,
            'label': invoice.invoice_number or f'#{invoice.id}',
            'sub': ('Covered by the khaata; paying it returns the advance'
                    if covered and owed < 1 else None),
            'date': invoice.date_issued.date().isoformat()
            if invoice.date_issued else None,
            'total': round(total),
            'room': room,
            'owed': round(owed),
            'covered': round(covered),
            'reserved': max(round(float(live_receipt_total(invoice))
                                  - float(invoice.receipts_paid or 0)), 0),
        })

    items.extend(_loan_items(business_id, party, party_id))
    return items


def _loan_items(business_id, party, party_id):
    """
    The person's open loans on this side, oldest first. `room` is what is
    left on each once every pending cheque clears, as for invoices: the
    reserving settlement is what a payment may still take.
    """
    Role = Customer if party == 'customer' else Supplier
    person_id = Role.objects.filter(pk=party_id).values_list('person_id', flat=True).first()
    loans = list(
        Transaction.objects
        .filter(business_id=business_id, type=LOAN_TYPE[party], status='C',
                party_payment__person_id=person_id)
        .order_by('date', 'created_at')
    )
    if not loans:
        return []
    cleared = settle_parties(business_id, party, [party_id]).get(party_id)
    reserving = settle_parties(business_id, party, [party_id], include_pending=True).get(party_id)
    items = []
    for loan in loans:
        held = reserving.loan_status(loan.id) if reserving else None
        owed = cleared.loan_status(loan.id) if cleared else None
        room = int(held['remaining']) if held else 0
        if room <= 0:
            continue
        items.append({
            'kind': LOAN,
            'id': loan.id,
            'label': f"{LABELS[LOAN_TYPE[party]]}{f' {loan.reference}' if loan.reference else ''}",
            'sub': None,
            'date': loan.date.isoformat(),
            'total': loan.amount,
            'room': room,
            'reserved': max((owed['remaining'] if owed else 0) - room, 0),
        })
    return items


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
                         item_ids, money_details, context, order='oldest',
                         person=None):
    """
    Write one payment atomically: one transaction per ticked invoice it
    reaches, then one on-account transaction for whatever is left.

    Delegates every write to TransactionCreateSerializer, which already owns
    the type/source rules, the invoice-room check and the delegation to
    SalesReceipt / PurchaseReceipt / PartyPayment. This function owns only the
    split, the group id and the all-or-nothing boundary.

    `item_ids` is the list of {kind, id} the user ticked, possibly empty.
    `party_id` may be None when the person has no role on this side; then
    only the other side's advance can be paid back. Returns the created
    transactions, named ones in allocation order, then the refund of the
    other side's advance, the on-account one last.
    """
    from .serializers import TransactionCreateSerializer

    if person is None and party_id is not None:
        Role = Customer if party == 'customer' else Supplier
        person = Role.objects.select_related('person').get(pk=party_id).person

    available = ({(item['kind'], item['id']): item
                  for item in settleable_items(business_id, party, party_id)}
                 if party_id is not None else {})

    chosen = []
    for ref in item_ids:
        key = (ref['kind'], int(ref['id']))
        item = available.get(key)
        if item is not None:
            chosen.append(item)

    applied_rows, left = allocate(chosen, amount, order)

    # Named payments first: each is validated against its invoice's room,
    # and the on-account part, once written, would reach the oldest invoices
    # and shrink that room.
    parts = [(item['kind'], item['id'], applied) for item, applied in applied_rows]
    advance_role, advance_room = other_side_advance(business_id, party, person)
    refund = min(left, advance_room)
    if refund > 0:
        parts.append((ADVANCE, advance_role, refund))
        left -= refund
    if left > 0:
        if party_id is None:
            raise serializers.ValidationError({'amount': (
                f'Only {advance_room:,} can be paid here, the advance on the other '
                f'side. Add the {party} role to record more.')})
        parts.append((ON_ACCOUNT, party_id, left))
    person_id = None
    if any(kind == LOAN for kind, _, _ in parts):
        Role = Customer if party == 'customer' else Supplier
        person_id = Role.objects.filter(pk=party_id).values_list('person_id', flat=True).first()

    group = uuid.uuid4()
    types = ITEM_TYPES[party]
    invoice_field = INVOICE_FIELDS[party]
    created = []

    with db_transaction.atomic():
        for kind, target_id, applied in parts:
            payload = dict(money_details)
            payload['amount'] = applied

            if kind == LOAN:
                # A repayment naming its loan; a loan names the person.
                payload['type'] = Transaction.LOAN_REPAID_BY[LOAN_TYPE[party]]
                payload['person'] = person_id
                payload['loan'] = target_id
            elif kind == ADVANCE:
                other_kind, refund_type = ADVANCE_REFUND[party]
                payload['type'] = refund_type
                payload[other_kind] = target_id
            elif kind == ON_ACCOUNT:
                payload['type'] = types[kind]
                payload[party] = target_id
            else:
                payload['type'] = types[kind]
                payload[invoice_field] = target_id

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
