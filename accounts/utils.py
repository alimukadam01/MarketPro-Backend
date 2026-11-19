from datetime import date as date_cls, timedelta

from django.db.models import Q, Sum
from django.utils import timezone

from inventory.models import InventoryItem
from root.models import Customer, Expense, Person, Supplier
from root.utils import local_date
from sales.models import (
    PurchaseInvoice, PurchaseReceipt, SalesInvoice, SalesInvoiceItem,
    SalesReceipt
)
from .models import MoneyAccount, PartyOpeningBalance, PartyPayment, Transaction
from .settlement import (
    LABELS, Settlement, on_account_paid_on_issue_date, settle_parties,
    settle_party, settle_people,
)


MODULE = 'accounting'

# A receipt settles nothing until its money has cleared. Receipts with no
# transaction behind them predate the accounting module and always count.
CLEARED_RECEIPT = (
    Q(transaction_record__isnull=True) | Q(transaction_record__status='C')
)

# The two questions on-account money gets asked. CLEARED_ONLY is what a party
# owes; LIVE_ON_ACCOUNT is what a payment picker may still offer, and counts
# pending cheques because they have already claimed that money. Mirrors
# CLEARED_RECEIPT vs sales.utils.LIVE_RECEIPT on the invoice side.
CLEARED_ONLY = ('C',)
LIVE_ON_ACCOUNT = ('C', 'PEN')

def party_effect_totals(business_id, party_filter, party, statuses=CLEARED_ONLY,
                        money_only=False):
    """
    One party's khaata rows that name no invoice (Transaction.PARTY_EFFECTS),
    as (settles, adds): what takes debt off their balance - on-account
    payments, credit notes - and what puts it back on - refunds, debit notes.

    `statuses` answers one of two different questions, and they need different
    answers - exactly as amount_paid and invoice_room differ on the invoice
    side:

      CLEARED_ONLY   what the party actually owes. A pending cheque has paid
                     nothing yet, so it must not reduce a balance.
      ('PEN',)       what a pending cheque has already claimed, which a
                     payment picker must hold back, or the same debt can be
                     settled twice.

    Notes are always cleared, so they only ever appear in the first. Pass
    money_only to leave them out entirely.
    """
    types = Transaction.party_types(party, money_only=money_only)
    rows = (
        PartyPayment.objects
        .filter(business_id=business_id, transaction__status__in=statuses,
                transaction__type__in=types, **party_filter)
        .values('transaction__type')
        .annotate(total=Sum('transaction__amount'))
    )
    settles = adds = 0
    for row in rows:
        _, effect, _ = Transaction.PARTY_EFFECTS[row['transaction__type']]
        if effect == 'settles':
            settles += row['total'] or 0
        else:
            adds += row['total'] or 0
    return settles, adds


def receipt_date(receipt):
    """
    The day the money moved, not the day the row was typed. Backdating is
    unrestricted, so these can differ.
    """
    transaction = getattr(receipt, 'transaction_record', None)
    if transaction and transaction.date:
        return transaction.date
    return local_date(receipt.created_at)

# Invoices in these states never happened. Reporting (revenue, purchase value,
# targets) excludes only these.
CANCELLED_SALES_STATUSES = ['X']
CANCELLED_PURCHASE_STATUSES = ['C']

# Invoices that are not debts on the khaata: cancelled ones never happened and
# drafts are not confirmed yet (credit-debit-notes plan, D11). A khaata rule
# only - reporting keeps counting drafts as it always has.
NOT_A_DEBT_SALES_STATUSES = (
    SalesInvoice.CANCELLED_STATUSES + SalesInvoice.DRAFT_STATUSES)
NOT_A_DEBT_PURCHASE_STATUSES = (
    PurchaseInvoice.CANCELLED_STATUSES + PurchaseInvoice.DRAFT_STATUSES)


def has_accounting_access(request, business):
    """
    Accounting data is invisible without accounting permission (BRD rule 9).
    Admins are gated by the paid add-on flag on BusinessConfig; employees by
    their permission matrix.
    """
    user = request.user

    if not user or not user.is_authenticated or not business:
        return False

    if user.role == 'admin':
        try:
            return bool(business.config.accounting)
        except Exception as error:
            print(error)
            return False

    try:
        employee = user.emp_records.filter(business_id=business.id).first()
        if not employee:
            return False
        return bool(employee.access.permissions.get(MODULE, {}).get('view'))
    except Exception as error:
        print(error)
        return False


def parse_date(value, fallback=None):
    if not value:
        return fallback
    try:
        return date_cls.fromisoformat(str(value)[:10])
    except ValueError:
        return fallback


def month_bounds(day=None):
    day = day or timezone.localdate()
    start = day.replace(day=1)
    return start, day


def previous_month_bounds(day=None):
    day = day or timezone.localdate()
    last_day_prev = day.replace(day=1) - timedelta(days=1)
    return last_day_prev.replace(day=1), last_day_prev


# ── Party balances ────────────────────────────────────────────────────────────

def party_invoiced(business_id, customer=None, supplier=None,
                   date_from=None, date_to=None):
    """
    Everything billed to a party that is a debt: cancelled invoices never
    happened and drafts are not confirmed yet, so neither counts. The figure
    every balance starts from.

    date_from / date_to bound it on the invoice date, for the Total Business
    card's period. Balances never pass them: a balance is everything, ever.
    """
    if customer:
        invoices = (
            SalesInvoice.objects
            .filter(business_id=business_id, customer_id=customer.id)
            .exclude(status__in=NOT_A_DEBT_SALES_STATUSES)
        )
    else:
        invoices = (
            PurchaseInvoice.objects
            .filter(business_id=business_id, supplier_id=supplier.id)
            .exclude(status__in=NOT_A_DEBT_PURCHASE_STATUSES)
        )

    if date_from:
        invoices = invoices.filter(date_issued__date__gte=date_from)
    if date_to:
        invoices = invoices.filter(date_issued__date__lte=date_to)
    return invoices.aggregate(total=Sum('total'))['total'] or 0


def _party_balance(business_id, kind, party, receipts):
    """
    invoiced - paid against invoices - what settles + what adds
             + opening balance

    "What settles" is on-account payments and credit notes; "what adds" is
    refunds and debit notes (Transaction.PARTY_EFFECTS).

    Three aggregates rather than a settlement pass, because list screens call
    this once per party. The engine's balance is the same figure by
    construction; accounts.tests_settlement asserts it.

    `paid` counts every cleared receipt, including one on a draft or
    cancelled invoice: that money was still received. The engine keeps it as
    credit for the same reason.
    """
    # By person: the side also carries the person's loans and set-offs, which
    # name no role (register #28). The types keep it to this side.
    party_filter = {'person_id': party.person_id}
    invoiced = party_invoiced(business_id, **{kind: party})
    paid = receipts.filter(CLEARED_RECEIPT).aggregate(
        total=Sum('amount'))['total'] or 0
    settles, adds = party_effect_totals(business_id, party_filter, kind)

    opening = getattr(party, 'opening_balance', None)
    opening_amount = opening.amount if opening else 0

    return round(invoiced - paid - settles + adds + opening_amount)


def customer_balance(customer, business_id):
    """
    What a customer owes (negative: what the business owes them).
    """
    receipts = SalesReceipt.objects.filter(
        sales_invoice__business_id=business_id,
        sales_invoice__customer_id=customer.id)
    return _party_balance(business_id, 'customer', customer, receipts)


def supplier_balance(supplier, business_id):
    """
    What the business owes a supplier (negative: what they owe the business),
    mirroring customer_balance.
    """
    receipts = PurchaseReceipt.objects.filter(
        purchase_invoice__business_id=business_id,
        purchase_invoice__supplier_id=supplier.id)
    return _party_balance(business_id, 'supplier', supplier, receipts)


def aging_buckets(business_id, customer):
    """
    What a customer owes, split by how long it has been owed.

    Read straight off the khaata settlement (accounts/settlement.py): the
    debts left after the pool has settled the opening balance first and then
    the oldest debts, each aged from its own date. The buckets always add up
    to the balance.
    """
    return settle_party(business_id, 'customer', customer.id).aging()


def _khaata_sides(business_id):
    """
    Every party's settled khaata, split by who owes whom (plan D10).

    Receivables: customers who owe the business, and suppliers who owe it
    (more credit than debt - a rebate, an advance). Payables: the mirror.
    Four queries per party kind, however many parties.
    """
    receivable, payable = [], []
    # select_related: each row reads its name and phone from the person.
    kinds = (
        ('customer', Customer.objects.filter(business_id=business_id)
         .select_related('person')),
        ('supplier', Supplier.objects.filter(business_id=business_id)
         .select_related('person')),
    )
    for kind, parties in kinds:
        settled = settle_parties(business_id, kind)
        for party in parties:
            khaata = settled.get(party.id)
            if khaata is None:
                continue
            balance = round(khaata.balance)
            if balance == 0:
                continue
            owes_business = balance > 0 if kind == 'customer' else balance < 0
            row = {
                'id': party.id,
                'party_kind': kind,
                'person_id': party.person_id,
                'name': party.name,
                'business_name': getattr(party, 'business_name', None),
                'phone': party.phone,
                'balance': abs(balance),
                'aging': khaata.aging(),
            }
            (receivable if owes_business else payable).append(row)

        # People with no role on this side whose loans or set-offs still put
        # them on it - a lender who is neither customer nor supplier (#28).
        # They have no role id; party_kind says which side the row is from.
        loans_only = settle_people(business_id, kind)
        people = Person.objects.in_bulk(list(loans_only))
        for person_id, khaata in loans_only.items():
            balance = round(khaata.balance)
            if balance == 0 or person_id not in people:
                continue
            person = people[person_id]
            owes_business = balance > 0 if kind == 'customer' else balance < 0
            (receivable if owes_business else payable).append({
                'id': None,
                'party_kind': kind,
                'person_id': person_id,
                'name': person.name,
                'business_name': person.business_name,
                'phone': person.phone,
                'balance': abs(balance),
                'aging': khaata.aging(),
            })
    return receivable, payable


def receivables(business_id):
    """
    Everyone who owes the business, largest first, plus aging (§6.5) - the
    customers with a balance, and any supplier holding the business's money.
    """
    rows, _ = _khaata_sides(business_id)
    totals = {'current': 0, 'days_31_60': 0, 'days_over_60': 0}
    for row in rows:
        for key in totals:
            totals[key] += row['aging'][key]

    rows.sort(key=lambda row: row['balance'], reverse=True)
    return {
        'total': sum(row['balance'] for row in rows),
        'parties': rows,
        'aging': totals,
    }


def payables(business_id):
    """
    Everyone the business owes, largest first - suppliers with a balance, and
    any customer it holds money for (an overpayment, a credit note).
    """
    _, rows = _khaata_sides(business_id)
    rows.sort(key=lambda row: row['balance'], reverse=True)
    return {'total': sum(row['balance'] for row in rows), 'parties': rows}


def opening_balance_remaining(business_id, party, party_id, opening=None,
                              include_pending=False, settlement=None,
                              pending=None):
    """
    What is still unsettled on ONE party's opening balance.

    Returns (opening_balance, settled, remaining); remaining is never negative,
    and a party with no opening balance is (0, 0, 0).

    The pool settles the opening balance before anything else (plan D5), so
    "settled" is whatever of it the khaata's on-account payments and credit
    notes have reached - read from the settlement, not summed separately.

    include_pending=True also holds back on-account money sitting in a pending
    cheque, which is what a payment picker must use - otherwise a cheque for
    the whole opening balance leaves it still on offer, and clearing the
    cheque after a second payment settles the same debt twice. A pending
    cheque would settle the opening balance first when it clears, so that is
    where it is reserved. Leave it False for anything that reports what the
    party owes.

    uncleared_opening_balances below calls this, and so does settleable_items,
    so the party page and the dialog can never disagree. Pass `opening` when
    the row is already in hand to save a query, and likewise `settlement` (the
    party's settle_party result) and `pending` (its pending on-account money):
    a list works them out for every party at once.
    """
    if opening is None:
        opening = (
            PartyOpeningBalance.objects
            .filter(business_id=business_id, **{f'{party}_id': party_id})
            .first()
        )

    if opening is None:
        return 0, 0, 0

    opening_amount = opening.amount or 0
    if opening_amount <= 0:
        return opening_amount, 0, 0

    if settlement is None:
        settlement = settle_party(business_id, party, party_id)
    remaining = settlement.opening_remaining()
    settled = opening_amount - remaining
    if include_pending:
        if pending is None:
            # By person, so a pending loan repayment is held back too.
            pending, _ = party_effect_totals(
                business_id, {f'person__{party}__id': party_id}, party,
                statuses=('PEN',), money_only=True,
            )
        settled += min(pending, remaining)
        remaining = max(remaining - pending, 0)

    return opening_amount, round(settled), max(round(remaining), 0)


def uncleared_opening_balances(business_id, party):
    """
    Parties whose pre-MarketPro opening balance has not been settled yet, with
    what is left on it.

    Deliberately NOT the same figure as payables()/receivables(). Those are the
    whole relationship - invoices included. This is only the balance carried
    across from the paper khaata at onboarding. The khaata's pool (on-account
    payments, credit notes, a credit opening balance, overpayments) settles it
    before anything else, so:

        remaining = opening balance - what the pool has reached of it

    party is 'customer' or 'supplier'.
    """
    rows = []
    total = 0

    openings = (
        PartyOpeningBalance.objects
        .filter(business_id=business_id, amount__gt=0)
        .exclude(**{f'{party}__isnull': True})
        .select_related(f'{party}__person')
    )
    party_ids = [getattr(opening, f'{party}_id') for opening in openings]
    settlements = settle_parties(business_id, party, party_ids)
    # By person (then back to the role), so a pending loan repayment is
    # held back with the rest of the person's pending money on this side.
    pending_payment = {
        row[f'person__{party}__id']: row['total'] or 0
        for row in PartyPayment.objects
        .filter(business_id=business_id, transaction__status='PEN',
                transaction__type__in=Transaction.party_types(
                    party, 'settles', money_only=True),
                **{f'person__{party}__id__in': party_ids})
        .values(f'person__{party}__id')
        .annotate(total=Sum('transaction__amount'))
    }

    for opening in openings:
        who = getattr(opening, party)
        settlement = settlements.get(who.id) or Settlement(party, who.id)

        # This is a payment PICKER, so `balance` is what can still be
        # allocated, not what is still owed. A pending cheque has claimed its
        # share already; offering it again would settle the same debt twice.
        opening_amount, settled, owed = opening_balance_remaining(
            business_id, party, who.id, opening=opening, settlement=settlement)
        _, claimed, remaining = opening_balance_remaining(
            business_id, party, who.id, opening=opening, include_pending=True,
            settlement=settlement, pending=pending_payment.get(who.id, 0))

        if remaining <= 0:
            continue

        total += remaining
        rows.append({
            'id': who.id,
            'name': who.name,
            'business_name': getattr(who, 'business_name', None),
            'phone': who.phone,
            'opening_balance': opening_amount,
            'settled': settled,
            'owed': owed,
            'reserved': max(claimed - settled, 0),
            'balance': remaining,
        })

    rows.sort(key=lambda row: row['balance'], reverse=True)
    return {'total': total, 'parties': rows}


# ── Khaata ────────────────────────────────────────────────────────────────────

def _side_rows(business_id, kind, role, person):
    """
    One side of a person's khaata, unsorted: the role's opening balance,
    invoices and invoice payments (when they have the role), and every money
    row of this side naming the person - on-account payments, notes,
    refunds, loans and set-offs. Naam is what puts the side up; jama what
    brings it down.
    """
    rows = []
    opening = getattr(role, 'opening_balance', None) if role else None
    if opening:
        rows.append({
            'date': opening.as_of_date.isoformat(),
            '_seq': '',
            'description': 'Opening balance',
            'reference': None,
            'naam': opening.amount if opening.amount > 0 else 0,
            'jama': -opening.amount if opening.amount < 0 else 0,
        })

    if role is not None and kind == 'customer':
        invoices = (
            SalesInvoice.objects
            .filter(business_id=business_id, customer_id=role.id)
            .exclude(status__in=NOT_A_DEBT_SALES_STATUSES)
        )
        for invoice in invoices:
            rows.append({
                'date': local_date(invoice.date_issued).isoformat()
                if invoice.date_issued else None,
                '_seq': invoice.created_at.isoformat(),
                'description': 'Sales invoice',
                'reference': invoice.invoice_number or str(invoice.id),
                'naam': round(invoice.total or 0),
                'jama': 0,
            })

        receipts = SalesReceipt.objects.filter(
            sales_invoice__business_id=business_id,
            sales_invoice__customer_id=role.id,
        ).filter(CLEARED_RECEIPT).select_related(
            'sales_invoice', 'transaction_record')
        for receipt in receipts:
            rows.append({
                'date': receipt_date(receipt).isoformat(),
                '_seq': receipt.created_at.isoformat(),
                'description': 'Payment received',
                'reference': receipt.sales_invoice.invoice_number
                or str(receipt.sales_invoice_id),
                'naam': 0,
                'jama': round(receipt.amount or 0),
            })

    if role is not None and kind == 'supplier':
        invoices = (
            PurchaseInvoice.objects
            .filter(business_id=business_id, supplier_id=role.id)
            .exclude(status__in=NOT_A_DEBT_PURCHASE_STATUSES)
        )
        for invoice in invoices:
            rows.append({
                # date_issued, not created_at: a backdated purchase has to sit
                # on the day it happened, the same way the sales branch above
                # dates its invoices, or the khaata and the purchase targets
                # disagree about which period an invoice belongs to.
                'date': local_date(invoice.date_issued).isoformat()
                if invoice.date_issued else None,
                '_seq': invoice.created_at.isoformat(),
                'description': 'Purchase invoice',
                'reference': invoice.invoice_number or str(invoice.id),
                'naam': round(invoice.total or 0),
                'jama': 0,
            })

        receipts = PurchaseReceipt.objects.filter(
            purchase_invoice__business_id=business_id,
            purchase_invoice__supplier_id=role.id,
        ).filter(CLEARED_RECEIPT).select_related(
            'purchase_invoice', 'transaction_record')
        for receipt in receipts:
            rows.append({
                'date': receipt_date(receipt).isoformat(),
                '_seq': receipt.created_at.isoformat(),
                'description': 'Payment made',
                'reference': receipt.purchase_invoice.invoice_number
                or str(receipt.purchase_invoice_id),
                'naam': 0,
                'jama': round(receipt.amount or 0),
            })

    # Everything that names no invoice, by person, so loans and set-offs are
    # here whatever roles the person has. A payment, credit note, recovery or
    # set-off lowers this side; a refund, debit note or loan raises it.
    descriptions = {
        t: (LABELS[entry_kind], 'jama' if effect == 'settles' else 'naam')
        for t, (p, effect, entry_kind) in Transaction.PARTY_EFFECTS.items()
        if p == kind
    }
    on_account = PartyPayment.objects.filter(
        business_id=business_id, transaction__status='C',
        transaction__type__in=list(descriptions), person_id=person.id,
    ).select_related('transaction')
    for payment in on_account:
        txn = payment.transaction
        description, side = descriptions[txn.type]
        rows.append({
            'date': txn.date.isoformat(),
            '_seq': payment.created_at.isoformat(),
            'description': description,
            'reference': txn.set_off_reference or txn.reference,
            'naam': txn.amount if side == 'naam' else 0,
            'jama': txn.amount if side == 'jama' else 0,
        })

    return [row for row in rows if row['date']]


def _finish_ledger(rows, date_from, date_to, sides=None):
    """
    Sort, carry everything before date_from in as one figure, and add the
    running balance. With `sides` (the both-sides view) each side keeps its
    own running balance and brought-forward figure - never one netted
    balance (register #25).
    """
    rows.sort(key=lambda row: (row['date'], row['_seq']))
    for row in rows:
        del row['_seq']

    keys = sides or [None]
    brought_forward = {key: 0 for key in keys}
    if date_from:
        cutoff = date_from.isoformat()
        for row in rows:
            if row['date'] < cutoff:
                brought_forward[row.get('side')] += row['naam'] - row['jama']
        rows = [row for row in rows if row['date'] >= cutoff]
    if date_to:
        rows = [row for row in rows if row['date'] <= date_to.isoformat()]

    running = dict(brought_forward)
    for row in rows:
        running[row.get('side')] += row['naam'] - row['jama']
        if sides:
            for key in sides:
                row[f'{key}_balance'] = running[key]
        else:
            row['balance'] = running[None]

    if sides:
        return {
            'rows': rows,
            'brought_forward': {key: round(value) for key, value in brought_forward.items()},
            'closing_balance': {key: round(value) for key, value in running.items()},
        }
    return {
        'rows': rows,
        'brought_forward': round(brought_forward[None]),
        'closing_balance': running[None],
    }


def party_ledger(business_id, customer=None, supplier=None,
                 date_from=None, date_to=None):
    """
    One role's khaata: every invoice raised and every payment made, in date
    order, with a running balance - plus the person's loans and set-offs on
    that side. Naam is what they owe; jama is what they have paid.
    """
    kind = 'customer' if customer else 'supplier'
    role = customer or supplier
    return _finish_ledger(_side_rows(business_id, kind, role, role.person),
                          date_from, date_to)


# The khaata's sides by their People names (register #27).
SIDE_KINDS = {'receivable': 'customer', 'payable': 'supplier'}


def person_ledger(business_id, person, side='both', date_from=None, date_to=None):
    """
    A person's khaata (register #25): side='receivable' or 'payable' gives
    one side with one running balance; 'both' gives every row tagged with its
    side and a running balance per side.
    """
    from root.people import role_of

    if side in SIDE_KINDS:
        kind = SIDE_KINDS[side]
        return _finish_ledger(
            _side_rows(business_id, kind, role_of(person, kind), person),
            date_from, date_to)

    rows = []
    for name, kind in SIDE_KINDS.items():
        for row in _side_rows(business_id, kind, role_of(person, kind), person):
            row['side'] = name
            rows.append(row)
    return _finish_ledger(rows, date_from, date_to, sides=list(SIDE_KINDS))


# ── Roznamcha ─────────────────────────────────────────────────────────────────

def day_book(business_id, day, date_to=None):
    """
    The roznamcha: one day, grouped by account, with opening and closing
    balances and totals (§6.7).

    With date_to it covers day..date_to - the Book of Business - opening on
    the balance the night before `day` and closing on the one at date_to.
    """
    date_to = date_to or day
    previous_day = day - timedelta(days=1)
    transactions = Transaction.objects.day_book(business_id, day, date_to)

    accounts = (MoneyAccount.objects.for_business(business_id).active()
                .with_balance(previous_day))
    sections = []
    total_in = 0
    total_out = 0

    for account in accounts:
        rows = []
        account_in = 0
        account_out = 0

        for txn in transactions:
            involved = (
                txn.account_id == account.id
                or txn.transfer_account_id == account.id
            )
            if not involved:
                continue

            if txn.transfer_account_id == account.id:
                # Money arriving from another of the shop's own accounts.
                inflow, outflow = txn.amount, 0
            elif txn.direction == 'in':
                inflow, outflow = txn.amount, 0
            elif txn.direction == 'out' or txn.type == 'transfer':
                inflow, outflow = 0, txn.amount
            else:
                inflow = txn.amount if txn.amount > 0 else 0
                outflow = -txn.amount if txn.amount < 0 else 0

            if txn.status == 'C':
                account_in += inflow
                account_out += outflow

            rows.append({
                'id': txn.id,
                'date': txn.date.isoformat() if txn.date else None,
                'type': txn.type,
                'amount': txn.amount,
                'payment_method': txn.payment_method,
                'status': txn.status,
                'reference': txn.reference,
                'notes': txn.notes,
                'money_in': inflow,
                'money_out': outflow,
            })

        opening = account.balance_value
        sections.append({
            'account_id': account.id,
            'account_name': account.name,
            'account_type': account.type,
            'opening_balance': opening,
            'rows': rows,
            'money_in': account_in,
            'money_out': account_out,
            'closing_balance': opening + account_in - account_out,
        })

        total_in += account_in
        total_out += account_out

    return {
        'date': day.isoformat(),
        'date_from': day.isoformat(),
        'date_to': date_to.isoformat(),
        'accounts': sections,
        'total_money_in': total_in,
        'total_money_out': total_out,
    }


def _with_other(total, lines):
    """
    The named lines, an `other` line for whatever they leave out (refunds,
    other payments, transfers the day book counts), and the total - so the
    lines always add up to the total (register #8).
    """
    other = round(total - sum(lines.values()))
    return {**lines, 'other': other, 'total': total}


def daily_summary(business_id, day, date_to=None):
    """
    The end-of-day picture (§6.9) — what came in, what went out, what is left,
    and what is owed.

    With date_to it summarises day..date_to (the Book of Business). Receivable
    and payable stay as they stand today: they are balances, not flows.
    """
    date_to = date_to or day
    book = day_book(business_id, day, date_to)
    # The receivable and payable totals of receivables() and payables(), from
    # one settlement of every khaata rather than one each.
    receivable_rows, payable_rows = _khaata_sides(business_id)
    totals = Transaction.objects.totals_by_type(business_id, day, date_to)
    salary_expenses = Transaction.objects.salary_expense_total(
        business_id, day, date_to)

    credit_extended = (
        SalesInvoice.objects
        .filter(business_id=business_id,
                date_issued__date__gte=day, date_issued__date__lte=date_to)
        # A draft extends no credit yet (plan D11).
        .exclude(status__in=NOT_A_DEBT_SALES_STATUSES)
        .aggregate(total=Sum('total'))['total'] or 0
    )
    # Cash sales are money that pays an invoice on its issue date: a payment
    # naming it that day, or on-account money the khaata settles onto it from
    # the same day. Whatever an invoice is not paid that day is credit
    # extended, and anything paid on it later is udhaar recovered - otherwise
    # it read as a sale and, subtracted from credit_extended, hid the credit
    # actually given.
    named_later = Transaction.objects.sale_payments_after_issue(
        business_id, day, date_to)
    on_account_same_day = on_account_paid_on_issue_date(
        business_id, 'customer', day, date_to)
    cash_sales = (totals.get('sale_payment', 0) - named_later
                  + on_account_same_day)
    udhaar_recovered = (totals.get('customer_receipt', 0) - on_account_same_day
                        + named_later)

    cheques_due = [
        {
            'id': txn.id,
            'cheque_number': txn.cheque_number,
            'amount': txn.amount,
            'due_date': txn.cheque_due_date.isoformat()
            if txn.cheque_due_date else None,
            'type': txn.type,
        }
        for txn in Transaction.objects.pending_cheques(business_id)
        .filter(cheque_due_date__lte=date_to)
    ]

    return {
        'date': day.isoformat(),
        'date_from': day.isoformat(),
        'date_to': date_to.isoformat(),
        'accounts': [
            {
                'account_name': section['account_name'],
                'opening_balance': section['opening_balance'],
                'closing_balance': section['closing_balance'],
            }
            for section in book['accounts']
        ],
        'money_in': _with_other(book['total_money_in'], {
            'cash_sales': cash_sales,
            'udhaar_recovered': udhaar_recovered,
            # Loans have their own line (register #8): money borrowed, and
            # money lent out coming back. Neither is income.
            'loans': (totals.get('loan_received', 0)
                      + totals.get('loan_recovered', 0)),
            'other_income': (
                totals.get('other_income', 0)
                + totals.get('owner_capital', 0)
                + totals.get('purchase_return_refund', 0)
            ),
        }),
        'money_out': _with_other(book['total_money_out'], {
            # Salaries are expenses in the salary category now, not their own
            # type, so their share moves out of the expenses line and into the
            # salaries one. Without this the expenses line would quietly
            # absorb them and salaries would read 0 (BRD 6.9 wants both).
            # Legacy salary_payment rows keep counting, so history is unchanged.
            'expenses': totals.get('expense', 0) - salary_expenses,
            'supplier_payments': (
                totals.get('supplier_payment', 0)
                + totals.get('purchase_payment', 0)
            ),
            'salaries': totals.get('salary_payment', 0) + salary_expenses,
            'drawings': totals.get('owner_drawings', 0),
            # Loans repaid, and money lent out.
            'loans': (totals.get('loan_repayment', 0)
                      + totals.get('loan_given', 0)),
        }),
        'credit_extended': round(max(credit_extended - cash_sales, 0)),
        'total_receivable': sum(row['balance'] for row in receivable_rows),
        'total_payable': sum(row['balance'] for row in payable_rows),
        'pending_cheques': cheques_due,
        'profit': profit_estimate(business_id, day, date_to),
    }


# ── Munafa ────────────────────────────────────────────────────────────────────

def profit_estimate(business_id, date_from, date_to):
    """
    Revenue less cost of goods sold less expenses. An estimate only — it is
    exactly as accurate as the data entered (§6.11).

    Extra charges are in revenue, because `total` carries them, and each one
    is also recorded as an "invoice_extra_charge" expense (sync_charge_expenses
    in sales/utils.py), which the expense total below picks up. The two cancel,
    so a charge nets to nothing in profit.
    """
    revenue = (
        SalesInvoice.objects
        .filter(business_id=business_id,
                date_issued__date__gte=date_from,
                date_issued__date__lte=date_to)
        .exclude(status__in=CANCELLED_SALES_STATUSES)
        .aggregate(total=Sum('total'))['total'] or 0
    )

    cost_map = {
        item['product_id']: item['unit_cost'] or 0
        for item in InventoryItem.objects
        .filter(business_id=business_id)
        .values('product_id', 'unit_cost')
    }

    items = (
        SalesInvoiceItem.objects
        .filter(sales_invoice__business_id=business_id,
                sales_invoice__date_issued__date__gte=date_from,
                sales_invoice__date_issued__date__lte=date_to)
        .exclude(sales_invoice__status__in=CANCELLED_SALES_STATUSES)
    )
    cogs = sum(item.net_quantity * cost_map.get(item.product_id, 0)
               for item in items)

    # The transaction linked to an expense owns that expense's date, because
    # Expense itself has no date field and backdating is unrestricted. Dating
    # these by created_at - the row's insertion timestamp - put a backdated
    # expense in a different month here than in day_book and daily_summary,
    # which read Transaction. It also contradicted the salary line below, which
    # has always dated through Transaction.
    #
    # An expense with no transaction behind it predates the accounting module;
    # it keeps its created_at so it is not silently dropped by the join.
    expenses = (
        Expense.objects
        .filter(business_id=business_id)
        .with_expense_date()
        .in_range('expense_date', date_from, date_to)
        .aggregate(total=Sum('amount'))['total'] or 0
    )

    by_type = Transaction.objects.totals_by_type(business_id, date_from, date_to)
    salaries = by_type.get('salary_payment', 0)

    # Notes, dated by their own date (credit-debit-notes plan, 5.5). A credit
    # note to a customer gives back revenue and a debit note adds to it. A
    # supplier's credit note (a target rebate) is money the business keeps, so
    # it adds to profit on its own line rather than through COGS, which is
    # read from current unit costs; a supplier's debit note takes it away.
    revenue += (by_type.get('customer_debit_note', 0)
                - by_type.get('customer_credit_note', 0))
    supplier_credits = (by_type.get('supplier_credit_note', 0)
                        - by_type.get('supplier_debit_note', 0))

    return {
        'revenue': round(revenue),
        'cost_of_goods_sold': round(cogs),
        'expenses': round(expenses + salaries),
        'supplier_credits': round(supplier_credits),
        'profit': round(revenue - cogs - expenses - salaries + supplier_credits),
    }
