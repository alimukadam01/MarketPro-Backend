from collections import defaultdict
from datetime import date as date_cls, timedelta

from django.db.models import F, Q, Sum
from django.utils import timezone

from inventory.models import InventoryItem
from root.models import Customer, Expense, Person, Supplier
from root.utils import local_date
from sales.models import (
    PurchaseInvoice, PurchaseReceipt, SalesInvoice, SalesInvoiceItem,
    SalesReceipt
)
from .models import MoneyAccount, OpeningNet, PartyOpeningBalance, PartyPayment, Transaction
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
# drafts are not confirmed yet (credit-debit-notes plan, D11). Profit and the
# sales targets use the same rule (fix plan 1.2).
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
        opening = OpeningNet.of(
            PartyOpeningBalance.objects
            .filter(business_id=business_id)
            .of_role(party, party_id)
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

    # Each role's openings on its own khaata, netted; only what comes out
    # owed is an opening balance to clear.
    Role = Customer if party == 'customer' else Supplier
    by_role = defaultdict(list)
    for row in (PartyOpeningBalance.objects
                .filter(business_id=business_id, kind=party,
                        **{f'person__{party}__isnull': False})
                .annotate(_role=F(f'person__{party}__id'))):
        by_role[row._role].append(row)
    nets = {role_id: OpeningNet.of(found) for role_id, found in by_role.items()}
    nets = {role_id: net for role_id, net in nets.items() if net.amount > 0}
    roles = Role.objects.filter(pk__in=list(nets)).select_related('person')
    openings = [(role, nets[role.id]) for role in roles]
    party_ids = list(nets)
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

    for who, opening in openings:
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

# Same-day order on the khaata (fix plan 4, rule 1): what the person stood at,
# then what they took on, then what they paid, then notes and refunds. Typing
# order only breaks a tie inside one of these.
WALK_ORDER = {
    'opening': 0,
    'invoice': 1, 'loan_given': 1, 'loan_received': 1,
    'payment': 2, 'loan_recovered': 2, 'loan_repayment': 2, 'set_off': 2,
    'credit_note': 3, 'debit_note': 3, 'refund': 3,
}


def _walk_key(row):
    """The order the khaata walks its rows in, oldest first."""
    return (row['date'], row.get('_order', 2), row['_seq'])


def _side_rows(business_id, kind, role, person):
    """
    One khaata of a person, unsorted: their opening balances on it,
    invoices and invoice payments (when they have the role), and every money
    row of this side naming the person - on-account payments, notes,
    refunds, loans and set-offs. Naam is what puts the side up; jama what
    brings it down.
    """
    rows = []
    # The person's opening balances that count on this khaata: one row each,
    # an advance (a Payable opening of a customer, say) as jama.
    openings = PartyOpeningBalance.objects.filter(
        business_id=business_id, person_id=person.id, kind=kind)
    for opening in openings:
        signed = opening.signed_amount
        rows.append({
            'date': opening.as_of_date.isoformat(),
            '_seq': '',
            '_order': WALK_ORDER['opening'],
            'description': 'Opening balance',
            'reference': None,
            'naam': signed if signed > 0 else 0,
            'jama': -signed if signed < 0 else 0,
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
                '_order': WALK_ORDER['invoice'],
                '_invoice': f's{invoice.id}',
                'description': 'Sales invoice',
                'reference': invoice.invoice_number or str(invoice.id),
                'naam': invoice.total or 0,
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
                '_order': WALK_ORDER['payment'],
                # The invoice this payment names (fix plan 4, rule 2).
                '_for': f's{receipt.sales_invoice_id}',
                'description': 'Payment received',
                'reference': receipt.sales_invoice.invoice_number
                or str(receipt.sales_invoice_id),
                'naam': 0,
                'jama': receipt.amount or 0,
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
                '_order': WALK_ORDER['invoice'],
                '_invoice': f'p{invoice.id}',
                'description': 'Purchase invoice',
                'reference': invoice.invoice_number or str(invoice.id),
                'naam': invoice.total or 0,
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
                '_order': WALK_ORDER['payment'],
                '_for': f'p{receipt.purchase_invoice_id}',
                'description': 'Payment made',
                'reference': receipt.purchase_invoice.invoice_number
                or str(receipt.purchase_invoice_id),
                'naam': 0,
                'jama': receipt.amount or 0,
            })

    # Everything that names no invoice, by person, so loans and set-offs are
    # here whatever roles the person has. A payment, credit note, recovery or
    # set-off lowers this side; a refund, debit note or loan raises it.
    descriptions = {
        t: (LABELS[entry_kind], 'jama' if effect == 'settles' else 'naam',
            WALK_ORDER.get(entry_kind, 2))
        for t, (p, effect, entry_kind) in Transaction.PARTY_EFFECTS.items()
        if p == kind
    }
    on_account = PartyPayment.objects.filter(
        business_id=business_id, transaction__status='C',
        transaction__type__in=list(descriptions), person_id=person.id,
    ).select_related('transaction')
    for payment in on_account:
        txn = payment.transaction
        description, side, order = descriptions[txn.type]
        rows.append({
            'date': txn.date.isoformat(),
            '_seq': payment.created_at.isoformat(),
            '_order': order,
            'description': description,
            'reference': txn.set_off_reference or txn.reference,
            'naam': txn.amount if side == 'naam' else 0,
            'jama': txn.amount if side == 'jama' else 0,
        })

    return [row for row in rows if row['date']]


# Debit and Credit, the way the books put them (fix plan 3.1). Naam is what
# raises a side and jama what lowers it. On Receivable (they owe you) a raise
# is a Debit; on Payable (you owe them) a raise is a Credit.
def _debit_credit(row, side):
    if side == 'payable':
        return row['jama'], row['naam']
    return row['naam'], row['jama']


# Working keys of the walk, never sent.
WALK_KEYS = ('_seq', '_order', '_invoice', '_for')


def _finish_ledger(rows, date_from, date_to, sides=None, side=None):
    """
    Sort, carry everything before date_from in as one figure, and add the
    running balance. With `sides` (the both-sides view) each side keeps its
    own running balance and brought-forward figure - never one netted
    balance (register #25). `side` names the one side otherwise. Every row
    gets its Debit and Credit for its side.
    """
    rows.sort(key=_walk_key)
    for row in rows:
        for key in WALK_KEYS:
            row.pop(key, None)
        row['debit'], row['credit'] = _debit_credit(row, row.get('side') or side)

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
        'side': side,
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
    rows = _rounded_running(_side_rows(business_id, kind, role, role.person))
    return _finish_ledger(rows, date_from, date_to, side=NATURAL_SIDE[kind])


def _rounded_running(rows):
    """
    Whole-rupee rows that add up to the rounded balance. Invoice totals and
    receipts carry fractions (FloatField); rounding each row on its own and
    adding those up drifts from the cards, which add the exact amounts and
    round once (2026-11-23 production check: 1 and 2 PKR). So each row shows
    how far it moves the rounded running balance instead: one row may read a
    rupee off its own rounded amount, but the khaata always closes on the
    card's figure.
    """
    rows.sort(key=_walk_key)
    balance = 0
    for row in rows:
        before = round(balance)
        balance += row['naam'] - row['jama']
        change = round(balance) - before
        row['naam'] = change if change > 0 else 0
        row['jama'] = -change if change < 0 else 0
    return rows


# The khaata's sides by their People names (register #27).
SIDE_KINDS = {'receivable': 'customer', 'payable': 'supplier'}


def person_ledger(business_id, person, side='both', date_from=None, date_to=None):
    """
    A person's khaata (register #25), by the person's two sides: side=
    'receivable' or 'payable' gives one side with one running balance;
    'both' gives every row tagged with its side and a running balance per
    side.

    The sides are the person's, not the roles'. Each role's rows run on that
    role's khaata, and whatever takes it below zero is on the other side: a
    customer who pays 15,000 against 10,000 owed shows the payment as 10,000
    on Receivable and 5,000 on Payable (the advance), and the next sale uses
    the advance up there first. So neither side's running balance goes
    negative, and each closes on what the cards show (root.people
    .side_balances). It is still one transaction: only the khaata shows it
    in two parts.
    """
    rows = _person_side_rows(business_id, person)
    if side in SIDE_KINDS:
        rows = [row for row in rows if row.pop('side') == side]
        return _finish_ledger(rows, date_from, date_to, side=side)
    return _finish_ledger(rows, date_from, date_to, sides=list(SIDE_KINDS))


NATURAL_SIDE = {kind: side for side, kind in SIDE_KINDS.items()}
OTHER_SIDE = {'receivable': 'payable', 'payable': 'receivable'}


def _person_side_rows(business_id, person):
    """
    Every khaata row of the person, put on the side it moves. Walks each
    role's khaata in date order with its signed running balance: the part of
    a row that moves the balance on the role's own side stays there, and the
    part past zero goes to the other side, marked as the advance it makes or
    uses. A row that does both appears twice, once per side, with the same
    date and reference.

    An invoice takes all its own payments first (owner's rule, 27 Nov 2026),
    as the settlement does: the part its payments will cover waits on the
    role's own side for them and never touches an advance. Only what they
    leave unpaid is open to the advance. So a customer with an advance who
    pays an invoice separately keeps the advance, with no advance rows.

    Amounts are whole rupees: each part is how far the row moves the ROUNDED
    running balance (see _rounded_running), so each side closes on exactly
    the card's figure, which rounds each role's balance once.
    """
    from root.people import role_of

    out = []
    for kind, own in NATURAL_SIDE.items():
        other = OTHER_SIDE[own]
        rows = _side_rows(business_id, kind, role_of(person, kind), person)
        rows.sort(key=_walk_key)
        invoice_dates = {row['_invoice']: row['date']
                         for row in rows if row.get('_invoice')}

        def early(row):
            """A payment on an invoice, dated before the invoice."""
            target = row.get('_for')
            return bool(target and row['jama'] and not row['naam']
                        and invoice_dates.get(target, '') > row['date'])

        # What each invoice's own payments come to (on or after its date):
        # the part of the invoice that waits for them.
        own_paid = defaultdict(float)
        for row in rows:
            if row.get('_for') in invoice_dates and not early(row):
                own_paid[row['_for']] += row['jama'] - row['naam']

        # Payments made on an invoice before its date (fix plan 4, rule 2;
        # old records only, as such payments are refused now) are kept for
        # that invoice: an advance on the other side that no other invoice
        # can use. Exact amounts; each row shows how far it moves the rounded
        # total, like the balance below.
        held = {}
        held_as = {}
        # balance: the role's khaata less what invoices hold for their own
        # payments (waiting); waiting sits on the role's own side.
        balance = 0
        waiting = 0
        waits_for = {}

        def sides():
            return max(balance, 0) + waiting, max(-balance, 0)

        def walk(row, change, description, past_zero=None, wait=0):
            """Move the role's balance; put each part on the side it moves."""
            nonlocal balance, waiting
            own_before, other_before = sides()
            balance += change
            waiting += wait
            own_after, other_after = sides()
            own_part = round(own_after) - round(own_before)
            other_part = round(other_after) - round(other_before)
            if other_part < 0 <= own_part:
                # A row that uses up an advance (owner, 28 Nov 2026): it shows
                # at its full amount on its own side, then the advance moves
                # across as its own pair - applied on this side, used on the
                # other - the way an accountant books it. Same balances as
                # showing only the part the advance didn't cover.
                used = -other_part
                out.append(_part_row(row, own, own_part + used, description))
                out.append(_part_row(row, own, -used, description + ' · advance applied'))
                out.append(_part_row(row, other, other_part, description + ' · advance used'))
                return True
            for part_side, part in ((own, own_part), (other, other_part)):
                if not part:
                    continue
                text = description
                if part_side == other:
                    text = past_zero or (description + (
                        ' · advance' if part > 0 else ' · against advance'))
                out.append(_part_row(row, part_side, part, text))
            return own_part or other_part

        for row in rows:
            target = row.get('_for')
            if early(row):
                before = round(sum(held.values()))
                held[target] = held.get(target, 0) + row['jama']
                held_as[target] = row['description']
                part = round(sum(held.values())) - before
                out.append(_part_row(
                    row, other, part,
                    row['description'] + ' · advance for its invoice'))
                continue

            change = row['naam'] - row['jama']
            wait = 0
            invoice = row.get('_invoice')
            if invoice and row['naam'] > 0:
                # The part its own payments will pay waits for them.
                wait = min(row['naam'], max(own_paid.get(invoice, 0), 0))
                waits_for[invoice] = wait
            elif target in waits_for and row['jama'] > 0:
                # Its own payment clears what was waiting for it.
                wait = -min(row['jama'], waits_for[target])
                waits_for[target] += wait
            if not walk(row, change - wait, row['description'], wait=wait):
                out.append({**row, 'side': own, 'naam': 0, 'jama': 0})

            # The invoice its advance was kept for: the invoice stands at its
            # full amount, then the advance is applied against it, all of it.
            # Any of it past the invoice carries on like other money.
            if invoice in held:
                paid_as = held_as.pop(invoice)
                before = round(sum(held.values()))
                used = held.pop(invoice)
                released = round(sum(held.values())) - before
                walk(row, -used, paid_as + ' · advance applied',
                     past_zero=paid_as + ' · advance')
                if released:
                    out.append(_part_row(
                        row, other, released, paid_as + ' · advance used'))
        # Every kept invoice is in `rows` and comes after its advance, and
        # every invoice's own payments clear what waits for them, so `held`
        # and `waiting` are empty here and each side closes on the card's
        # figure.
    return out


def _part_row(row, side, part, description):
    """One side's part of a khaata row: a positive part raises that side."""
    return {
        **row,
        'side': side,
        'description': description,
        'naam': part if part > 0 else 0,
        'jama': -part if part < 0 else 0,
    }


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
    # The shop's money in and out. A transfer between two of its own accounts
    # is neither: it shows on both accounts' tables and moves both balances,
    # but stays out of these totals (fix plan 1.1).
    total_in = 0
    total_out = 0
    transfers = 0
    # Cash counts, by direction, so the summary can name them (fix plan 1.1).
    adjustments_in = 0
    adjustments_out = 0

    for account in accounts:
        rows = []
        account_in = 0
        account_out = 0
        opening = account.balance_value
        # Worked out oldest-first, in the order the money moved; only cleared
        # money moves it, so a pending cheque leaves the balance as it was
        # (fix plan 3.2). The screen shows the rows newest-first.
        balance = opening

        for txn in transactions:
            involved = (
                txn.account_id == account.id
                or txn.transfer_account_id == account.id
            )
            if not involved:
                continue

            is_transfer = txn.type == 'transfer'
            if txn.transfer_account_id == account.id:
                # Money arriving from another of the shop's own accounts.
                inflow, outflow = txn.amount, 0
            elif txn.direction == 'in':
                inflow, outflow = txn.amount, 0
            elif txn.direction == 'out' or is_transfer:
                inflow, outflow = 0, txn.amount
            else:
                inflow = txn.amount if txn.amount > 0 else 0
                outflow = -txn.amount if txn.amount < 0 else 0

            if txn.status == 'C':
                account_in += inflow
                account_out += outflow
                balance += inflow - outflow
                if is_transfer:
                    # Counted once, on the account it left.
                    transfers += outflow
                else:
                    total_in += inflow
                    total_out += outflow
                    if txn.type == 'cash_adjustment':
                        adjustments_in += inflow
                        adjustments_out += outflow

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
                'balance': balance,
            })

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

    return {
        'date': day.isoformat(),
        'date_from': day.isoformat(),
        'date_to': date_to.isoformat(),
        'accounts': sections,
        'total_money_in': total_in,
        'total_money_out': total_out,
        'total_transfers': transfers,
        'cash_adjustments_in': adjustments_in,
        'cash_adjustments_out': adjustments_out,
    }


def _with_other(total, lines):
    """
    The named lines, an `other` line for whatever they leave out (refunds,
    other payments), and the total - so the lines always add up to the total
    (register #8). Transfers between the shop's own accounts are in neither
    the lines nor the total (fix plan 1.1).
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
            # A cash count that found more than the books said.
            'cash_adjustments': book['cash_adjustments_in'],
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
            # A cash count that found less.
            'cash_adjustments': book['cash_adjustments_out'],
        }),
        # Moved between the shop's own accounts in the period: not money in or
        # out, so not in either total.
        'transfers': book['total_transfers'],
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
        # A draft isn't a sale yet: the same rule as the khaata (fix plan 1.2).
        .exclude(status__in=NOT_A_DEBT_SALES_STATUSES)
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
        .exclude(sales_invoice__status__in=NOT_A_DEBT_SALES_STATUSES)
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
