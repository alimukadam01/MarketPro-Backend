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
from dataclasses import dataclass, field
from datetime import date as date_cls
from typing import Optional

from django.utils import timezone

from root.models import Customer, Supplier
from root.utils import local_date
from sales.models import PurchaseInvoice, SalesInvoice

from .models import PartyOpeningBalance, PartyPayment, Transaction


# Money compared below this is treated as equal. Invoice totals are floats;
# transaction amounts are whole rupees.
EPSILON = 0.005

# Per party kind: the invoice model and its foreign key, and which transaction
# types are pool money (credits) or pool debts. Notes are split by direction;
# refunds are money that went back out, so they put debt back on.
PARTY_SPEC = {
    'customer': {
        'invoice_model': SalesInvoice,
        'party_model': Customer,
        'credit_types': {'customer_receipt': 'payment',
                         'customer_credit_note': 'credit_note'},
        'debit_types': {'customer_debit_note': 'debit_note',
                        'sales_return_refund': 'refund'},
    },
    'supplier': {
        'invoice_model': PurchaseInvoice,
        'party_model': Supplier,
        'credit_types': {'supplier_payment': 'payment',
                         'supplier_credit_note': 'credit_note'},
        'debit_types': {'supplier_debit_note': 'debit_note',
                        'purchase_return_refund': 'refund'},
    },
}

LABELS = {
    'opening': 'Opening balance',
    'payment': 'On-account payment',
    'credit_note': 'Credit note',
    'debit_note': 'Debit note',
    'refund': 'Refund',
    'overpayment': 'Overpayment',
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
            if credit.kind in ('payment', 'credit_note')
            and credit.id == transaction_id
        ]

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


def settle_parties(business_id, party_kind, party_ids=None):
    """
    Settle every party of one kind in one business - or only `party_ids`.

    Three queries whatever the number of parties (openings, invoices with
    their receipts, on-account rows), grouped in Python. Returns
    {party_id: Settlement}; a party with nothing on its khaata is absent.
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

    for opening in PartyOpeningBalance.objects.filter(**scope):
        amount = opening.amount or 0
        if not amount:
            continue
        entry = Entry(
            kind='opening', id=None, label=LABELS['opening'],
            date=opening.as_of_date, amount=abs(amount), leads=True,
            seq=opening.created_at.isoformat() if opening.created_at else '')
        target = get(getattr(opening, party_field))
        (target.debts if amount > 0 else target.credits).append(entry)

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
        unpaid = billed - float(invoice.receipts_paid or 0)
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

    pool_types = {**spec['credit_types'], **spec['debit_types']}
    rows = (
        PartyPayment.objects
        .filter(**scope, transaction__status='C',
                transaction__type__in=list(pool_types))
        .select_related('transaction')
    )
    for row in rows:
        txn = row.transaction
        kind = pool_types[txn.type]
        entry = Entry(
            kind=kind, id=txn.id, label=LABELS[kind], date=txn.date,
            amount=txn.amount or 0, reference=txn.reference,
            seq=txn.created_at.isoformat() if txn.created_at else '')
        target = get(getattr(row, party_field))
        if txn.type in spec['credit_types']:
            target.credits.append(entry)
        else:
            target.debts.append(entry)

    for settlement in result.values():
        _match(settlement)
    return result


def settle_party(business_id, party_kind, party_id):
    """One party's khaata. Never None: an empty khaata settles to nothing."""
    return (settle_parties(business_id, party_kind, [party_id])
            .get(party_id) or Settlement(party_kind, party_id))


def _kind_of(invoice):
    return 'customer' if isinstance(invoice, SalesInvoice) else 'supplier'


def attach_settlement(invoices):
    """
    Set `_pool_applied` on each invoice - one khaata pass per party, however
    many of that party's invoices are on the page. Drafts and cancelled
    invoices get 0: they are not debts, so the pool never reaches them.

    Also sets `_settlement` so a caller can ask what settled the invoice
    without another pass.
    """
    groups = {}
    for invoice in invoices:
        kind = _kind_of(invoice)
        party_id = invoice.customer_id if kind == 'customer' else invoice.supplier_id
        groups.setdefault((invoice.business_id, kind), {}).setdefault(
            party_id, []).append(invoice)

    for (business_id, kind), by_party in groups.items():
        settled = settle_parties(business_id, kind, list(by_party))
        for party_id, party_invoices in by_party.items():
            settlement = settled.get(party_id) or Settlement(kind, party_id)
            for invoice in party_invoices:
                invoice._settlement = settlement
                invoice._pool_applied = settlement.applied_to_invoice(invoice.id)
    return invoices


def settlement_for_transaction(transaction):
    """The khaata a note or an on-account payment belongs to, or None."""
    party_payment = getattr(transaction, 'party_payment', None)
    if party_payment is None:
        return None
    if party_payment.customer_id:
        return settle_party(transaction.business_id, 'customer',
                            party_payment.customer_id)
    if party_payment.supplier_id:
        return settle_party(transaction.business_id, 'supplier',
                            party_payment.supplier_id)
    return None
