"""
People: the rules behind /people/ (register #29, R1-R35, S1-S11).

A Person is one person or firm; Customer and Supplier are the roles they play.
Only a person is ever deleted. A role is deactivated, never deleted on its
own, and deleting a person first shows everything that goes with it.

The money side follows the khaata split the Receivables and Payables screens
already use (accounts.utils._khaata_sides): a customer who owes, or a
supplier holding the business's money, is Receivable; a supplier the business
owes, or a customer's advance, is Payable.
"""
import re
from collections import defaultdict
from datetime import datetime

from django.db import transaction as db_transaction
from django.db.models import Max, Q
from django.db.models.deletion import Collector
from rest_framework.permissions import BasePermission

from accounts.models import MoneyAccount, PartyPayment, Transaction
from accounts.settlement import settle_party, settle_person_sides
from audit.helpers import audited_update
from sales.models import PurchaseInvoice, SalesInvoice

from .models import WALK_IN_CUSTOMER_NAME, Customer, Person, Supplier
from .utils import get_active_business, local_date

MODULE = 'people'
ROLES = {'customer': Customer, 'supplier': Supplier}


# ── Access ────────────────────────────────────────────────────────────────────

def has_people_access(request, business, action):
    """Admins may do anything; employees what their `people` row allows."""
    user = request.user
    if not user or not user.is_authenticated or not business:
        return False
    if user.role == 'admin':
        return True
    try:
        employee = user.emp_records.filter(business_id=business.id).first()
        if not employee:
            return False
        return bool(employee.access.permissions.get(MODULE, {}).get(action))
    except Exception as error:
        print(error)
        return False


class HasPeopleAccess(BasePermission):
    """
    The `people` module gate, checked here and not only in the client: a
    delete removes transactions. Each viewset action maps to one permission;
    merge needs delete, since the merged-away person is removed.
    """

    message = 'You do not have access to People.'
    ACTION_PERMISSION = {
        'list': 'view', 'retrieve': 'view', 'delete_preview': 'view',
        'duplicates': 'view',
        'create': 'create',
        'update': 'edit', 'partial_update': 'edit', 'roles': 'edit',
        'destroy': 'delete', 'bulk_delete': 'delete', 'merge': 'delete',
    }

    def has_permission(self, request, view):
        business = get_active_business(request)
        if not business:
            return True  # the viewset answers with the standard 400
        needed = self.ACTION_PERMISSION.get(getattr(view, 'action', None), 'view')
        return has_people_access(request, business, needed)


# ── Roles ─────────────────────────────────────────────────────────────────────

def role_of(person, kind):
    """The person's customer or supplier record, or None."""
    try:
        return getattr(person, kind)
    except ROLES[kind].DoesNotExist:
        return None


def is_walk_in(person):
    return person.name == WALK_IN_CUSTOMER_NAME and role_of(person, 'customer') is not None


class PeopleError(Exception):
    """A rule refused the request; the message is for the user."""


def role_balance(business_id, kind, role_id):
    return round(settle_party(business_id, kind, role_id).balance)


def change_role(person, kind, action):
    """add, activate or deactivate one role. Returns the role record."""
    if kind not in ROLES:
        raise PeopleError('Role must be customer or supplier.')
    role = role_of(person, kind)
    if action == 'add':
        if role is not None:
            raise PeopleError(f'{person.name} is already a {kind}.')
        return ROLES[kind].objects.create(business_id=person.business_id, person=person)
    if role is None:
        raise PeopleError(f'{person.name} is not a {kind}.')
    if action == 'activate':
        role.is_active = True
    elif action == 'deactivate':
        if is_walk_in(person) and kind == 'customer':
            raise PeopleError('The walk-in customer is needed for counter sales and stays active.')
        balance = role_balance(person.business_id, kind, role.id)
        if balance:
            side = 'Receivable' if (balance > 0) == (kind == 'customer') else 'Payable'
            raise PeopleError(
                f'{person.name} has a {side} balance of PKR {abs(balance):,} as a {kind}. '
                f'Settle it before deactivating the {kind} role.')
        role.is_active = False
    else:
        raise PeopleError('Action must be add, activate or deactivate.')
    role.save(update_fields=['is_active', 'updated_at'] if kind == 'customer' else ['is_active'])
    return role


# ── Balances ──────────────────────────────────────────────────────────────────

def side_balances(business_id, person_ids=None):
    """
    {person_id: {'receivable': x, 'payable': y}}: each person's two sides,
    from their roles and from loans and set-offs, which need no role.
    """
    result = defaultdict(lambda: {'receivable': 0, 'payable': 0})
    for person_id, sides in settle_person_sides(business_id, person_ids).items():
        for kind, khaata in sides.items():
            if khaata is None:
                continue
            balance = round(khaata.balance)
            if not balance:
                continue
            owes_business = balance > 0 if kind == 'customer' else balance < 0
            result[person_id]['receivable' if owes_business else 'payable'] += abs(balance)
    return result


def last_activity(business_id, person_ids):
    """The latest invoice or on-account payment date per person."""
    latest = {}

    def keep(person_id, when):
        if when is None:
            return
        # Invoices carry an instant, transactions a date.
        day = local_date(when) if isinstance(when, datetime) else when
        if person_id not in latest or day > latest[person_id]:
            latest[person_id] = day

    for row in (SalesInvoice.objects.filter(business_id=business_id, customer__person_id__in=person_ids)
                .values('customer__person_id').annotate(last=Max('date_issued'))):
        keep(row['customer__person_id'], row['last'])
    for row in (PurchaseInvoice.objects.filter(business_id=business_id, supplier__person_id__in=person_ids)
                .values('supplier__person_id').annotate(last=Max('date_issued'))):
        keep(row['supplier__person_id'], row['last'])
    for row in (PartyPayment.objects.filter(business_id=business_id, person_id__in=person_ids)
                .values('person_id').annotate(last=Max('transaction__date'))):
        keep(row['person_id'], row['last'])
    return latest


# ── Delete ────────────────────────────────────────────────────────────────────

# What the preview calls each kind of record, and which amount it totals.
DELETE_LABELS = {
    'root.person': ('Person', None),
    'root.customer': ('Customer role', None),
    'root.supplier': ('Supplier role', None),
    'sales.salesinvoice': ('Sales invoices', 'total'),
    'sales.salesinvoiceitem': ('Sales invoice lines', None),
    'sales.purchaseinvoice': ('Purchase invoices', 'total'),
    'sales.purchaseinvoiceitem': ('Purchase invoice lines', None),
    'sales.salesreceipt': ('Payments received on sales invoices', 'amount'),
    'sales.purchasereceipt': ('Payments made on purchase invoices', 'amount'),
    'accounts.transaction': ('Transactions (money records and notes)', 'amount'),
    'accounts.partypayment': ('Links from transactions to this person', None),
    'accounts.partyopeningbalance': ('Opening balances', 'amount'),
    'sales.purchasequotationitem': ('Purchase quotation lines', None),
    'sales.returneditem': ('Returned items', None),
    'projects.projectsalesinvoice': ('Project links to sales invoices', None),
    'projects.projectpurchaseinvoice': ('Project links to purchase invoices', None),
}


def _party_transactions(person):
    """
    On-account money, notes, refunds, loans and set-offs that name this
    person. Their PartyPayment link cascades, but the Transaction it points to
    would not, so these are deleted explicitly - nothing about the person is
    left behind (register S1).
    """
    return Transaction.objects.filter(party_payment__person=person)


def _collect(person):
    collector = Collector(using='default')
    collector.collect([person])
    transactions = list(_party_transactions(person))
    if transactions:
        collector.collect(transactions)
    return collector


def _account_changes(business_id, transactions):
    """How each money account's balance moves once these transactions go."""
    change = defaultdict(int)
    for txn in transactions:
        if txn.status != 'C' or not txn.account_id:
            continue
        if txn.type in Transaction.IN_TYPES:
            change[txn.account_id] -= txn.amount
        elif txn.type in Transaction.OUT_TYPES:
            change[txn.account_id] += txn.amount
    if not change:
        return []
    accounts = MoneyAccount.objects.filter(business_id=business_id, id__in=change).with_balance()
    return [
        {'id': account.id, 'name': account.name,
         'balance_now': round(account.balance_value or 0),
         'change': change[account.id],
         'balance_after': round(account.balance_value or 0) + change[account.id]}
        for account in accounts.order_by('id')
    ]


def delete_preview(person):
    """
    Everything deleting this person removes, from the same Collector the
    delete itself runs - so the list shown is the list deleted.
    """
    collector = _collect(person)
    rows = {}
    for model, instances in collector.data.items():
        rows[model._meta.label_lower] = list(instances)
    for queryset in collector.fast_deletes:
        rows.setdefault(queryset.model._meta.label_lower, []).extend(queryset)

    items = []
    for label, instances in rows.items():
        name, amount_field = DELETE_LABELS.get(
            label, (instances[0]._meta.verbose_name_plural.capitalize(), None))
        row = {'model': label, 'label': name, 'count': len(instances)}
        if amount_field:
            row['total'] = round(sum(getattr(obj, amount_field) or 0 for obj in instances))
        items.append(row)
    order = list(DELETE_LABELS)
    items.sort(key=lambda row: order.index(row['model']) if row['model'] in order else len(order))

    # Records that stay, with the link to this person cleared (SET_NULL):
    # a project keeps going without its customer, for one.
    kept = []
    for (field, _value), batches in collector.field_updates.items():
        count = sum(len(batch) if isinstance(batch, (list, set, tuple)) else batch.count()
                    for batch in batches)
        if not count:
            continue
        model = field.model
        kept.append({
            'model': model._meta.label_lower,
            'label': f'{model._meta.verbose_name_plural.capitalize()} kept, '
                     f'no longer linked to {person.name}',
            'count': count,
        })
    from targets.models import Target
    subjects = Q(pk__in=[])
    for kind in ROLES:
        role = role_of(person, kind)
        if role is not None:
            subjects |= Q(subject_type=kind, subject_id=role.id)
    targets = Target.objects.filter(subjects, business_id=person.business_id)
    if targets.exists():
        kept.append({'model': 'targets.target', 'label': 'Targets kept with their label, '
                     'tracking nothing from now on', 'count': targets.count()})

    transactions = rows.get('accounts.transaction', [])
    return {
        'person': {'id': person.id, 'name': person.name},
        'can_delete': not is_walk_in(person),
        'reason': 'The walk-in customer is needed for counter sales and cannot be deleted.'
                  if is_walk_in(person) else None,
        'deleted': items,
        'kept': kept,
        'money_accounts': _account_changes(person.business_id, transactions),
        'stock_note': 'Deleting invoices does not put stock back or take it out.',
    }


def names_match(typed, name):
    return (typed or '').strip().casefold() == (name or '').strip().casefold()


def delete_person(person, confirm_name):
    if is_walk_in(person):
        raise PeopleError('The walk-in customer is needed for counter sales and cannot be deleted.')
    if not names_match(confirm_name, person.name):
        raise PeopleError(f'Type the name exactly as shown, "{person.name}", to delete.')
    with db_transaction.atomic():
        _collect(person).delete()


def has_records(person):
    """Whether deleting this person would take anything beyond the person and roles."""
    collector = _collect(person)
    simple = {'root.person', 'root.customer', 'root.supplier'}
    if any(model._meta.label_lower not in simple and instances
           for model, instances in collector.data.items()):
        return True
    return any(qs.exists() for qs in collector.fast_deletes
               if qs.model._meta.label_lower not in simple)


# ── Duplicates and merge ──────────────────────────────────────────────────────

def normalize_phone(phone):
    """0300-1234567, +92 300 1234567 and 923001234567 all read 03001234567."""
    digits = re.sub(r'\D', '', phone or '')
    if digits.startswith('0092'):
        digits = '0' + digits[4:]
    elif digits.startswith('92') and len(digits) == 12:
        digits = '0' + digits[2:]
    elif len(digits) == 10 and digits.startswith('3'):
        digits = '0' + digits
    return digits if len(digits) >= 7 else ''


def duplicates(business_id):
    """Groups of people sharing a phone number, walk-in left out (register #17)."""
    groups = defaultdict(list)
    people = (Person.objects.filter(business_id=business_id)
              .exclude(phone__isnull=True).exclude(phone='').order_by('id'))
    for person in people:
        if is_walk_in(person):
            continue
        key = normalize_phone(person.phone)
        if key:
            groups[key].append(person)
    return [{'phone': key, 'people': found} for key, found in groups.items() if len(found) > 1]


def _move_related(source, target, skip=()):
    """Point every record that names `source` at `target` instead."""
    for rel in source._meta.related_objects:
        if rel.many_to_many or rel.get_accessor_name() in skip:
            continue
        queryset = rel.related_model._base_manager.filter(**{rel.field.name: source})
        if queryset.exists():
            audited_update(queryset, **{rel.field.name: target})


def merge(kept, other, name=None, phone=None):
    """
    Folds `other` into `kept` (register R29). A role only one of them has is
    handed over; a role both have is combined - everything naming the other's
    role moves to the kept one, which is then the only one. Two opening
    balances on the same side block it. The other person is deleted, now empty.
    """
    if kept.pk == other.pk:
        raise PeopleError('Pick two different people to merge.')
    if kept.business_id != other.business_id:
        raise PeopleError('These people belong to different businesses.')
    if is_walk_in(kept) or is_walk_in(other):
        raise PeopleError('The walk-in customer cannot be merged.')
    for kind in ROLES:
        mine, theirs = role_of(kept, kind), role_of(other, kind)
        if mine and theirs and hasattr(mine, 'opening_balance') and hasattr(theirs, 'opening_balance'):
            side = 'Receivable' if kind == 'customer' else 'Payable'
            raise PeopleError(
                f'Both have a {side} opening balance. Clear one of them before merging.')

    from targets.models import Target
    with db_transaction.atomic():
        for kind in ROLES:
            mine, theirs = role_of(kept, kind), role_of(other, kind)
            if theirs is None:
                continue
            if mine is None:
                theirs.person = kept
                theirs.save(update_fields=['person'])
                continue
            _move_related(theirs, mine)
            audited_update(Target.objects.filter(subject_type=kind, subject_id=theirs.id),
                           subject_id=mine.id)
            if theirs.is_active and not mine.is_active:
                mine.is_active = True
                mine.save(update_fields=['is_active'])
            theirs.delete()

        other.refresh_from_db()
        _move_related(other, kept, skip=('customer', 'supplier'))

        if name is not None:
            kept.name = name
        if phone is not None:
            kept.phone = phone
        for field in ('phone', 'email', 'address', 'city', 'business_name', 'notes'):
            if not getattr(kept, field) and getattr(other, field):
                setattr(kept, field, getattr(other, field))
        kept.save()
        other.delete()
    return kept
