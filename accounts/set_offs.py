"""
Set-offs: one person's Receivable and Payable settled against each other by
the same amount, moving no money (register #15, #20, R25-R28).

A set-off is two transactions sharing a payment_group: set_off_receivable
lowers the side the person owes, set_off_payable the side the business owes.
Neither has an account or a payment method, so neither reaches a cash figure,
the Day Book or the Book of Business, exactly as notes do (R28). The khaata
settles each side oldest first, as it settles all money that names no invoice.

The reference is built when shown: SO-<YYYYMMDD>-<transaction id> (R27).
"""
import uuid

from django.db import transaction as db_transaction

from .models import PartyPayment, Transaction
from .settlement import settle_person_sides


class SetOffError(Exception):
    """A rule refused the set-off; the message is for the user."""


def set_off_room(business_id, person, excluding_group=None):
    """
    (receivable, payable): what each side can still be set off by.

    A set-off needs something owed on both sides: the person owes the
    business on Receivable and the business owes them on Payable. Editing a
    set-off first adds its own amount back, so it can be changed in place.
    """
    sides = settle_person_sides(business_id, [person.id]).get(person.id) or {}
    receivable = round(sides['customer'].balance) if sides.get('customer') else 0
    payable = round(sides['supplier'].balance) if sides.get('supplier') else 0
    if excluding_group is not None:
        current = (Transaction.objects
                   .filter(payment_group=excluding_group, type='set_off_receivable')
                   .values_list('amount', flat=True).first()) or 0
        receivable += current
        payable += current
    return max(receivable, 0), max(payable, 0)


def _check(business_id, person, amount, excluding_group=None):
    if not amount or amount <= 0:
        raise SetOffError('Amount must be greater than zero.')
    receivable, payable = set_off_room(business_id, person, excluding_group)
    if not receivable or not payable:
        raise SetOffError(
            f'A set-off needs a balance on both sides. {person.name} has '
            f'Receivable PKR {receivable:,} and Payable PKR {payable:,}.')
    limit = min(receivable, payable)
    if amount > limit:
        raise SetOffError(
            f'A set-off can be at most the smaller side, PKR {limit:,}.')


def record_set_off(business_id, user_id, person, amount, date, notes=None):
    """Write both halves together, or neither. Returns them, receivable first."""
    _check(business_id, person, amount)
    group = uuid.uuid4()
    created = []
    with db_transaction.atomic():
        for txn_type in Transaction.SET_OFF_TYPES:
            txn = Transaction.objects.create(
                business_id=business_id, type=txn_type, amount=amount,
                date=date, status='C', account=None, payment_method=None,
                notes=notes, payment_group=group, created_by_id=user_id)
            PartyPayment.objects.create(
                business_id=business_id, transaction=txn, person=person)
            created.append(txn)
    return created


def set_off_group(business_id, group):
    """The two halves of one set-off, receivable first; [] if not found."""
    rows = list(Transaction.objects
                .filter(business_id=business_id, payment_group=group,
                        type__in=Transaction.SET_OFF_TYPES)
                .select_related('party_payment__person'))
    rows.sort(key=lambda txn: Transaction.SET_OFF_TYPES.index(txn.type))
    return rows if len(rows) == 2 else []


def update_set_off(business_id, group, amount=None, date=None, notes=None):
    """Change both halves together (R27). Saves go through the audit log."""
    halves = set_off_group(business_id, group)
    if not halves:
        raise SetOffError('Set-off not found.')
    person = halves[0].party_payment.person
    if amount is not None:
        _check(business_id, person, amount, excluding_group=group)
    with db_transaction.atomic():
        for txn in halves:
            if amount is not None:
                txn.amount = amount
            if date is not None:
                txn.date = date
            if notes is not None:
                txn.notes = notes
            txn.save()
    return halves


def delete_set_off(business_id, group):
    """Remove both halves together; the khaata settles as if it never was."""
    halves = set_off_group(business_id, group)
    if not halves:
        raise SetOffError('Set-off not found.')
    with db_transaction.atomic():
        for txn in halves:
            txn.delete()
