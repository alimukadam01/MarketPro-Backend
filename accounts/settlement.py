"""
How a party's khaata settles itself (credit-debit-notes plan, section 4).

Every customer and supplier has one khaata. On every read, the money and credit
that does NOT name an invoice forms one pool, and the pool settles the party's
debts: the opening balance first, always, then everything else oldest-first.

    DEBTS                                    CREDITS (the pool)
      opening balance, if positive             opening balance, if negative
      each invoice's unpaid part               on-account payments
        (after its own receipts)               credit notes
      debit notes                              invoice overpayments
      refunds paid out

Nothing here is stored. A backdated invoice, a deleted note or a bounced cheque
simply changes what the next read computes - which is the point: the khaata
always reflects oldest-first as of now, and no stored allocation can go stale.

Payments that name an invoice are NOT pool money. They apply to their own
invoice first (SettledInvoiceMixin.receipts_paid), and the pool only sees what
they left unpaid. Because a named payment is capped at what the pool has not
covered (invoice_room), re-running both layers gives the same answer
whichever was entered first.

Only cleared money settles anything. A pending cheque reserves room in the
payment pickers (invoice_room, opening_balance_remaining) but is not in the
pool until it clears.

Invariant, asserted by the tests on every fixture:

    sum(remaining debts) - sum(leftover credits) == balance

Kept free of accounts.utils so utils can import this module.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date as date_cls
from typing import Optional

from django.db.models import F, Q
from django.utils import timezone

from root.models import Customer, Supplier
from root.utils import local_date
from sales.models import PurchaseInvoice, SalesInvoice

from .models import OpeningNet, PartyOpeningBalance, PartyPayment, Transaction


# Money compared below this is treated as equal. Invoice totals are floats;
# transaction amounts are whole rupees.
EPSILON = 0.005

def _pool_types(party_kind, effect):
    """{type: kind} for one party kind's PARTY_EFFECTS rows with this effect."""
    return {t: kind for t, (party, e, kind) in Transaction.PARTY_EFFECTS.items()
            if party == party_kind and e == effect}


# Per party kind: the invoice model and its foreign key, and which transaction
# types are pool money (credits) or pool debts - read from
# Transaction.PARTY_EFFECTS. Notes are split by direction; refunds are money
# that went back out, so they put debt back on.
PARTY_SPEC = {
    'customer': {
        'invoice_model': SalesInvoice,
        'party_model': Customer,
        'credit_types': _pool_types('customer', 'settles'),
        'debit_types': _pool_types('customer', 'adds'),
    },
    'supplier': {
        'invoice_model': PurchaseInvoice,
        'party_model': Supplier,
        'credit_types': _pool_types('supplier', 'settles'),
        'debit_types': _pool_types('supplier', 'adds'),
    },
}

# The entry kinds that are pool credit from a transaction (on-account money
# and credit notes), as opposed to an opening balance or an overpayment.
SETTLING_KINDS = frozenset(
    kind for _, effect, kind in Transaction.PARTY_EFFECTS.values()
    if effect == 'settles')

LABELS = {
    'opening': 'Opening balance',
    'payment': 'On-account payment',
    'credit_note': 'Credit note',
    'debit_note': 'Debit note',
    'refund': 'Refund',
    'overpayment': 'Overpayment',
    'loan_given': 'Loan given',
    'loan_recovered': 'Loan recovered',
    'loan_received': 'Loan received',
    'loan_repayment': 'Loan repaid',
    'set_off': 'Set-off',
}


def not_a_debt_statuses(invoice_model):
    """Cancelled invoices never happened; drafts are not confirmed yet."""
    return invoice_model.CANCELLED_STATUSES + invoice_model.DRAFT_STATUSES


@dataclass
class Entry:
    """One debt or one credit on the khaata."""
    kind: str                 # opening | invoice | debit_note | refund |
                              # payment | credit_note | overpayment
    id: Optional[int]         # invoice id or transaction id; None for opening
    label: str                # what a person would call it
    date: Optional[date_cls]
    amount: float             # what it contributes, always positive
    seq: str = ''             # tie-break for same-day entries (created_at)
    reference: Optional[str] = None
    remaining: float = 0.0    # after matching
    leads: bool = False       # the opening balance: always first
    repaid: float = 0.0       # a loan: what repayments naming it paid off

    def __post_init__(self):
        self.amount = round(float(self.amount), 2)
        self.remaining = self.amount

    @property
    def sort_key(self):
        return (0 if self.leads else 1, self.date or date_cls.min, self.seq)

    def as_dict(self):
        return {
            'kind': self.kind,
            'id': self.id,
            'label': self.label,
            'reference': self.reference,
            'date': self.date.isoformat() if self.date else None,
            'amount': round(self.amount),
            'remaining': round(self.remaining),
        }


@dataclass
class Settlement:
    """One party's khaata, settled."""
    party_kind: str
    party_id: int
    debts: list = field(default_factory=list)
    credits: list = field(default_factory=list)
    matches: list = field(default_factory=list)   # (credit, debt, amount)
    # The person behind it. For a khaata from loans alone (settle_people),
    # party_id is the person's id too.
    person_id: Optional[int] = None

    @property
    def balance(self):
        """What the party owes (negative: what the business owes them)."""
        return round(sum(d.amount for d in self.debts)
                     - sum(c.amount for c in self.credits), 2)

    @property
    def outstanding(self):
        return round(sum(d.remaining for d in self.debts), 2)

    @property
    def standing_credit(self):
        return round(sum(c.remaining for c in self.credits), 2)

    def applied_to_invoice(self, invoice_id):
        """How much of the pool reached this invoice."""
        return round(sum(amount for credit, debt, amount in self.matches
                         if debt.kind == 'invoice' and debt.id == invoice_id), 2)

    def settled_by(self, invoice_id):
        """The credits that settled this invoice, oldest first."""
        return [
            {**credit.as_dict(), 'applied': round(amount)}
            for credit, debt, amount in self.matches
            if debt.kind == 'invoice' and debt.id == invoice_id
        ]

    def settles(self, transaction_id):
        """What a credit (a note or an on-account payment) settled."""
        return [
            {**debt.as_dict(), 'applied': round(amount)}
            for credit, debt, amount in self.matches
            if credit.kind in SETTLING_KINDS
            and credit.id == transaction_id
        ]

    def loan_status(self, transaction_id):
        """
        A loan's standing (register R18): {'amount', 'repaid', 'remaining',
        'status'} with status 'open', 'part' or 'settled'; None if the loan
        is not on this khaata.
        """
        for debt in self.debts:
            if debt.id == transaction_id and debt.kind in Transaction.LOAN_REPAID_BY:
                full = round(debt.amount + debt.repaid, 2)
                remaining = round(debt.remaining, 2)
                repaid = round(full - remaining, 2)
                status = ('settled' if remaining <= EPSILON
                          else 'part' if repaid > EPSILON else 'open')
                return {'amount': round(full), 'repaid': round(repaid),
                        'remaining': round(remaining), 'status': status}
        return None

    def opening_remaining(self):
        for debt in self.debts:
            if debt.kind == 'opening':
                return round(debt.remaining, 2)
        return 0.0

    def aging(self, today=None):
        """
        Unsettled value by age. For a party who owes the business these are
        its remaining debts; for one the business owes - listed on the other
        side - it is its unused credit, aged from when it arose.
        """
        today = today or timezone.localdate()
        buckets = {'current': 0.0, 'days_31_60': 0.0, 'days_over_60': 0.0}
        entries = self.debts if self.balance >= 0 else self.credits
        for entry in entries:
            if entry.remaining <= EPSILON:
                continue
            age = (today - (entry.date or today)).days
            if age <= 30:
                buckets['current'] += entry.remaining
            elif age <= 60:
                buckets['days_31_60'] += entry.remaining
            else:
                buckets['days_over_60'] += entry.remaining
        return {key: round(value) for key, value in buckets.items()}


def _match(settlement):
    """Oldest credit against oldest debt, opening balance first."""
    debts = sorted(settlement.debts, key=lambda e: e.sort_key)
    credits = sorted(settlement.credits, key=lambda e: e.sort_key)
    settlement.debts, settlement.credits = debts, credits

    d = c = 0
    while d < len(debts) and c < len(credits):
        debt, credit = debts[d], credits[c]
        if debt.remaining <= EPSILON:
            d += 1
            continue
        if credit.remaining <= EPSILON:
            c += 1
            continue
        applied = round(min(debt.remaining, credit.remaining), 2)
        debt.remaining = round(debt.remaining - applied, 2)
        credit.remaining = round(credit.remaining - applied, 2)
        settlement.matches.append((credit, debt, applied))
    return settlement


def settle_parties(business_id, party_kind, party_ids=None,
                   include_pending=False):
    """
    Settle every party of one kind in one business - or only `party_ids`.

    Three queries whatever the number of parties (openings, invoices with
    their receipts, on-account rows), grouped in Python. Returns
    {party_id: Settlement}; a party with nothing on its khaata is absent.

    include_pending=True settles the khaata as it will stand once every
    pending cheque clears: each invoice is reduced by its pending named
    cheques too, and pending on-account cheques join the pool. Pending DEBITS
    (a refund cheque not yet cleared) stay out, so this view can only ever
    hold back more, never offer more. It is for payment pickers (invoice_room)
    only; anything that reports what a party owes uses the default.
    """
    spec = PARTY_SPEC[party_kind]
    Invoice = spec['invoice_model']
    party_field = f'{party_kind}_id'
    scope = {'business_id': business_id}
    if party_ids is not None:
        scope[f'{party_field}__in'] = list(party_ids)

    result = {}

    def get(party_id):
        if party_id not in result:
            result[party_id] = Settlement(party_kind, party_id)
        return result[party_id]

    _add_openings(business_id, party_kind, get, role_ids=party_ids)

    excluded = set(not_a_debt_statuses(Invoice))
    invoices = (
        Invoice.objects
        .filter(**scope)
        .prefetch_related('payment_receipts__transaction_record')
    )
    for invoice in invoices:
        # A draft or cancelled invoice is not a debt. Money already received
        # against one is still the party's money, though - the balance formula
        # counts every cleared receipt - so it stays on the khaata as credit.
        billed = 0.0 if invoice.status in excluded else float(invoice.total or 0)
        paid = invoice.receipts_live if include_pending else invoice.receipts_paid
        unpaid = billed - float(paid or 0)
        if abs(unpaid) <= EPSILON:
            continue
        issued = (local_date(invoice.date_issued)
                  if invoice.date_issued else timezone.localdate())
        label = invoice.invoice_number or f'#{invoice.id}'
        seq = invoice.created_at.isoformat() if invoice.created_at else ''
        target = get(getattr(invoice, party_field))
        if unpaid > 0:
            target.debts.append(Entry(
                kind='invoice', id=invoice.id, label=label,
                date=issued, amount=unpaid, seq=seq))
        else:
            # Paid more than it came to. The excess is still the party's
            # money, so it settles their next debt.
            target.credits.append(Entry(
                kind='overpayment', id=invoice.id,
                label=(f"Payment on {label} (not a debt)"
                       if invoice.status in excluded
                       else f"{LABELS['overpayment']} on {label}"),
                date=issued, amount=-unpaid, seq=seq))

    # The money rows are read by person: a role's khaata also carries its
    # person's loans and set-offs, which name only the person (register #28).
    # A person has at most one role per side, so each row's role is exact,
    # and it comes from the same query (the person's role on this side).
    _add_money_rows(business_id, party_kind, include_pending, get,
                    role_ids=party_ids)

    for settlement in result.values():
        _match(settlement)
    return result


def _add_openings(business_id, party_kind, get, role_ids=None,
                  people_without_role=False, person_ids=None):
    """
    The opening balances that count on this side's khaata, netted per person
    into one entry that leads: a debt when it comes out positive, credit when
    negative (an advance). Keyed like _add_money_rows: by the person's role on
    this side, or, with people_without_role, by person for those without it.
    """
    rows = PartyOpeningBalance.objects.filter(business_id=business_id, kind=party_kind)
    if people_without_role:
        rows = rows.filter(**{f'person__{party_kind}__isnull': True})
        if person_ids is not None:
            rows = rows.filter(person_id__in=list(person_ids))
        rows = rows.annotate(_key=F('person_id'))
    else:
        rows = rows.annotate(_key=F(f'person__{party_kind}__id'))
        rows = (rows.filter(_key__in=list(role_ids)) if role_ids is not None
                else rows.filter(_key__isnull=False))

    grouped = defaultdict(list)
    for row in rows:
        grouped[row._key].append(row)
    for key, openings in grouped.items():
        net = OpeningNet.of(openings)
        amount = net.amount
        if not amount:
            continue
        entry = Entry(
            kind='opening', id=None, label=LABELS['opening'],
            date=net.as_of_date, amount=abs(amount), leads=True,
            seq=net.created_at.isoformat() if net.created_at else '')
        target = get(key)
        (target.debts if amount > 0 else target.credits).append(entry)


def _add_money_rows(business_id, party_kind, include_pending, get,
                    role_ids=None, people_without_role=False, person_ids=None):
    """
    One side's PartyPayment rows (on-account money, notes, refunds, loans,
    set-offs) onto the settlements `get` returns.

    By default each row goes to its person's role on this side (`role_ids`
    narrows it); a person without that role is skipped. With
    people_without_role, the opposite: only people with no role on this side,
    keyed by person id (`person_ids` narrows it).

    A repayment that names its loan applies to that loan first, as a receipt
    applies to its invoice; any part beyond the loan stays on the khaata as
    credit.
    """
    spec = PARTY_SPEC[party_kind]
    pool_types = {**spec['credit_types'], **spec['debit_types']}
    counted = Q(transaction__status='C')
    if include_pending:
        counted |= Q(transaction__status='PEN',
                     transaction__type__in=list(spec['credit_types']))
    rows = (
        PartyPayment.objects
        .filter(counted, business_id=business_id,
                transaction__type__in=list(pool_types))
        .select_related('transaction')
    )
    role = f'person__{party_kind}__id'
    if people_without_role:
        rows = rows.filter(**{f'person__{party_kind}__isnull': True})
        if person_ids is not None:
            rows = rows.filter(person_id__in=list(person_ids))
        rows = rows.annotate(_key=F('person_id'))
    else:
        rows = rows.annotate(_key=F(role))
        rows = (rows.filter(_key__in=list(role_ids)) if role_ids is not None
                else rows.filter(_key__isnull=False))

    repayment_types = set(Transaction.LOAN_REPAID_BY.values())
    loans, named = {}, defaultdict(list)
    for row in rows:
        key = row._key
        txn = row.transaction
        kind = pool_types[txn.type]
        entry = Entry(
            kind=kind, id=txn.id, label=LABELS[kind], date=txn.date,
            amount=txn.amount or 0, reference=txn.reference,
            seq=txn.created_at.isoformat() if txn.created_at else '')
        target = get(key)
        if row.loan_id and txn.type in repayment_types:
            named[(key, row.loan_id)].append(entry)
        elif txn.type in spec['credit_types']:
            target.credits.append(entry)
        else:
            target.debts.append(entry)
            if txn.type in Transaction.LOAN_REPAID_BY:
                loans[(key, txn.id)] = entry

    for (key, loan_id), repayments in named.items():
        loan = loans.get((key, loan_id))
        if loan is None:
            # The loan is not on this khaata (deleted, or another side): the
            # money is still theirs, so it joins the pool like any payment.
            get(key).credits.extend(repayments)
            continue
        paid = round(sum(entry.amount for entry in repayments), 2)
        applied = min(paid, loan.amount)
        loan.amount = loan.remaining = round(loan.amount - applied, 2)
        loan.repaid += applied
        if paid - applied > EPSILON:
            get(key).credits.append(Entry(
                kind='overpayment', id=loan.id,
                label=f"Repaid beyond {LABELS[loan.kind].lower()} {loan.reference or f'#{loan.id}'}",
                date=repayments[-1].date, amount=paid - applied, seq=repayments[-1].seq))


def settle_party(business_id, party_kind, party_id):
    """One party's khaata. Never None: an empty khaata settles to nothing."""
    return (settle_parties(business_id, party_kind, [party_id])
            .get(party_id) or Settlement(party_kind, party_id))


def settle_people(business_id, party_kind, person_ids=None, include_pending=False):
    """
    One side's khaata for people who do NOT have that side's role - someone
    who only lent you money, say (register #28). Only their opening balance,
    loans and set-offs can be on it. Returns {person_id: Settlement}; anyone with the
    role is left out, because settle_parties already covers them.
    """
    result = {}

    def get(person_id):
        if person_id not in result:
            result[person_id] = Settlement(party_kind, person_id, person_id=person_id)
        return result[person_id]

    _add_openings(business_id, party_kind, get,
                  people_without_role=True, person_ids=person_ids)
    _add_money_rows(business_id, party_kind, include_pending, get,
                    people_without_role=True, person_ids=person_ids)
    for settlement in result.values():
        _match(settlement)
    return result


def settle_person_sides(business_id, person_ids=None):
    """
    {person_id: {'customer': Settlement | None, 'supplier': Settlement | None}}:
    both sides of each person, whether the side comes from a role or only
    from loans and set-offs.
    """
    sides = defaultdict(lambda: {'customer': None, 'supplier': None})
    for kind in PARTY_SPEC:
        Role = PARTY_SPEC[kind]['party_model']
        roles = Role.objects.filter(business_id=business_id)
        if person_ids is not None:
            roles = roles.filter(person_id__in=list(person_ids))
        person_of = dict(roles.values_list('id', 'person_id'))
        if person_of:
            for role_id, settlement in settle_parties(business_id, kind, list(person_of)).items():
                settlement.person_id = person_of[role_id]
                sides[person_of[role_id]][kind] = settlement
        for person_id, settlement in settle_people(business_id, kind, person_ids).items():
            sides[person_id][kind] = settlement
    return sides


def on_account_paid_on_issue_date(business_id, party_kind, date_from, date_to):
    """
    How much on-account money paid in the period the khaata settles onto an
    invoice issued the same day the money was paid.

    The daily summary counts that as a cash sale: it is the same money as a
    payment naming the invoice that day, only recorded without the tick. It
    is read from the khaata as it settles now, oldest first, so a same-day
    payment counts here only once the party's older debts are clear - exactly
    when it actually reaches that day's invoice.

    Only payments (customer_receipt / supplier_payment) count; a credit note
    or an overpayment is not money received that day.
    """
    spec = PARTY_SPEC[party_kind]
    payment_types = [code for code, kind in spec['credit_types'].items()
                     if kind == 'payment']
    party_field = f'{party_kind}_id'
    party_ids = set(
        PartyPayment.objects
        .filter(business_id=business_id, transaction__status='C',
                transaction__type__in=payment_types,
                transaction__date__gte=date_from,
                transaction__date__lte=date_to)
        .exclude(**{f'{party_field}__isnull': True})
        .values_list(party_field, flat=True)
    )
    if not party_ids:
        return 0

    total = 0.0
    for settlement in settle_parties(business_id, party_kind, party_ids).values():
        for credit, debt, applied in settlement.matches:
            if (credit.kind == 'payment' and debt.kind == 'invoice'
                    and credit.date is not None
                    and date_from <= credit.date <= date_to
                    and debt.date == credit.date):
                total += applied
    return round(total)


def _kind_of(invoice):
    return 'customer' if isinstance(invoice, SalesInvoice) else 'supplier'


def attach_settlement(invoices, include_pending=False):
    """
    Set `_pool_applied` on each invoice - one khaata pass per party, however
    many of that party's invoices are on the page. Drafts and cancelled
    invoices get 0: they are not debts, so the pool never reaches them.

    Also sets `_settlement` so a caller can ask what settled the invoice
    without another pass.

    include_pending=True runs the reserving pass instead (see settle_parties)
    and sets only `_reserved_pool_applied`, which invoice_room reads.
    """
    groups = {}
    for invoice in invoices:
        kind = _kind_of(invoice)
        party_id = invoice.customer_id if kind == 'customer' else invoice.supplier_id
        groups.setdefault((invoice.business_id, kind), {}).setdefault(
            party_id, []).append(invoice)

    for (business_id, kind), by_party in groups.items():
        settled = settle_parties(business_id, kind, list(by_party),
                                 include_pending=include_pending)
        for party_id, party_invoices in by_party.items():
            settlement = settled.get(party_id) or Settlement(kind, party_id)
            for invoice in party_invoices:
                applied = settlement.applied_to_invoice(invoice.id)
                if include_pending:
                    invoice._reserved_pool_applied = applied
                    continue
                invoice._settlement = settlement
                invoice._pool_applied = applied
    return invoices


def settlement_for_transaction(transaction):
    """
    The khaata a note, an on-account payment, a loan or a set-off belongs to,
    or None. Its side comes from the type; a person with no role on that side
    (a lender, say) has a khaata of loans alone.
    """
    party_payment = getattr(transaction, 'party_payment', None)
    if party_payment is None or transaction.type not in Transaction.PARTY_EFFECTS:
        return None
    kind = Transaction.PARTY_EFFECTS[transaction.type][0]
    role_id = (party_payment.customer_id if kind == 'customer'
               else party_payment.supplier_id)
    if role_id is None:
        Role = PARTY_SPEC[kind]['party_model']
        role_id = (Role.objects.filter(person_id=party_payment.person_id)
                   .values_list('id', flat=True).first())
    if role_id is not None:
        return settle_party(transaction.business_id, kind, role_id)
    return (settle_people(transaction.business_id, kind, [party_payment.person_id])
            .get(party_payment.person_id))
