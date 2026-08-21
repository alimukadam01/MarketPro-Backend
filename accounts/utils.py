from datetime import date as date_cls, timedelta

from django.db.models import Q, Sum
from django.utils import timezone

from inventory.models import InventoryItem
from root.models import Customer, Expense, Supplier
from root.utils import local_date
from sales.models import (
    PurchaseInvoice, PurchaseReceipt, SalesInvoice, SalesInvoiceItem,
    SalesReceipt
)
from .models import MoneyAccount, PartyOpeningBalance, PartyPayment, Transaction


MODULE = 'accounting'

# A receipt settles nothing until its money has cleared. Receipts with no
# transaction behind them predate the accounting module and always count.
CLEARED_RECEIPT = (
    Q(transaction_record__isnull=True) | Q(transaction_record__status='C')
)

# On-account money and which way it moves a party's balance. A payment settles
# what they owe; a refund is money going back out, so it puts the debt back on.
PARTY_ON_ACCOUNT = {
    'customer': {
        'payment': 'customer_receipt',
        'refund': 'sales_return_refund',
    },
    'supplier': {
        'payment': 'supplier_payment',
        'refund': 'purchase_return_refund',
    },
}


def on_account_totals(business_id, party_filter, kinds):
    """
    Cleared on-account money for one party, split into what settles their
    balance and what puts it back on.
    """
    rows = (
        PartyPayment.objects
        .filter(business_id=business_id, transaction__status='C',
                transaction__type__in=[kinds['payment'], kinds['refund']],
                **party_filter)
        .values('transaction__type')
        .annotate(total=Sum('transaction__amount'))
    )
    totals = {row['transaction__type']: row['total'] or 0 for row in rows}
    return totals.get(kinds['payment'], 0), totals.get(kinds['refund'], 0)


def receipt_date(receipt):
    """
    The day the money moved, not the day the row was typed. Backdating is
    unrestricted, so these can differ.
    """
    transaction = getattr(receipt, 'transaction_record', None)
    if transaction and transaction.date:
        return transaction.date
    return local_date(receipt.created_at)

# Invoices in these states never count toward what a party owes.
CANCELLED_SALES_STATUSES = ['X']
CANCELLED_PURCHASE_STATUSES = ['C']


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

def party_invoiced(business_id, customer=None, supplier=None):
    """
    Everything ever billed to a party, cancelled invoices excluded. This is the
    total business done with them, and the figure every balance starts from.
    """
    if customer:
        return (
            SalesInvoice.objects
            .filter(business_id=business_id, customer_id=customer.id)
            .exclude(status__in=CANCELLED_SALES_STATUSES)
            .aggregate(total=Sum('total'))['total'] or 0
        )

    return (
        PurchaseInvoice.objects
        .filter(business_id=business_id, supplier_id=supplier.id)
        .exclude(status__in=CANCELLED_PURCHASE_STATUSES)
        .aggregate(total=Sum('total'))['total'] or 0
    )


def customer_balance(customer, business_id):
    """
    What a customer owes: invoiced, less what they have paid, plus whatever
    they already owed on the paper khaata.
    """
    invoiced = party_invoiced(business_id, customer=customer)

    paid = (
        SalesReceipt.objects
        .filter(sales_invoice__business_id=business_id,
                sales_invoice__customer_id=customer.id)
        .filter(CLEARED_RECEIPT)
        .aggregate(total=Sum('amount'))['total'] or 0
    )

    settled, refunded = on_account_totals(
        business_id, {'customer_id': customer.id},
        PARTY_ON_ACCOUNT['customer'],
    )

    opening = getattr(customer, 'opening_balance', None)
    opening_amount = opening.amount if opening else 0

    return round(invoiced - paid - settled + refunded + opening_amount)


def supplier_balance(supplier, business_id):
    """
    What we owe a supplier, mirroring customer_balance.
    """
    invoiced = party_invoiced(business_id, supplier=supplier)

    paid = (
        PurchaseReceipt.objects
        .filter(purchase_invoice__business_id=business_id,
                purchase_invoice__supplier_id=supplier.id)
        .filter(CLEARED_RECEIPT)
        .aggregate(total=Sum('amount'))['total'] or 0
    )

    settled, refunded = on_account_totals(
        business_id, {'supplier_id': supplier.id},
        PARTY_ON_ACCOUNT['supplier'],
    )

    opening = getattr(supplier, 'opening_balance', None)
    opening_amount = opening.amount if opening else 0

    return round(invoiced - paid - settled + refunded + opening_amount)


def aging_buckets(business_id, customer_id):
    """
    Unpaid sales invoice value split by how long it has been outstanding.
    """
    today = timezone.localdate()
    buckets = {'current': 0, 'days_31_60': 0, 'days_over_60': 0}

    invoices = (
        SalesInvoice.objects
        .filter(business_id=business_id, customer_id=customer_id)
        .exclude(status__in=CANCELLED_SALES_STATUSES)
        .prefetch_related('payment_receipts__transaction_record')
    )

    for invoice in invoices:
        outstanding = (invoice.total or 0) - invoice.amount_paid
        if outstanding <= 0:
            continue

        issued = local_date(invoice.date_issued) if invoice.date_issued else today
        age = (today - issued).days

        if age <= 30:
            buckets['current'] += outstanding
        elif age <= 60:
            buckets['days_31_60'] += outstanding
        else:
            buckets['days_over_60'] += outstanding

    return {key: round(value) for key, value in buckets.items()}


def receivables(business_id):
    """
    Every customer with money outstanding, largest first, plus aging (§6.5).
    """
    rows = []
    total = 0
    totals = {'current': 0, 'days_31_60': 0, 'days_over_60': 0}

    customers = Customer.objects.filter(
        business_id=business_id).select_related('opening_balance')

    for customer in customers:
        balance = customer_balance(customer, business_id)
        if balance <= 0:
            continue

        total += balance
        buckets = aging_buckets(business_id, customer.id)
        for key in totals:
            totals[key] += buckets[key]

        rows.append({
            'id': customer.id,
            'name': customer.name,
            'phone': customer.phone,
            'balance': balance,
            'aging': buckets,
        })

    rows.sort(key=lambda row: row['balance'], reverse=True)
    return {'total': total, 'parties': rows, 'aging': totals}


def payables(business_id):
    """
    Every supplier we owe, largest first.
    """
    rows = []
    total = 0

    suppliers = Supplier.objects.filter(
        business_id=business_id).select_related('opening_balance')

    for supplier in suppliers:
        balance = supplier_balance(supplier, business_id)
        if balance <= 0:
            continue

        total += balance
        rows.append({
            'id': supplier.id,
            'name': supplier.name,
            'business_name': supplier.business_name,
            'phone': supplier.phone,
            'balance': balance,
        })

    rows.sort(key=lambda row: row['balance'], reverse=True)
    return {'total': total, 'parties': rows}


# ── Khaata ────────────────────────────────────────────────────────────────────

def party_ledger(business_id, customer=None, supplier=None,
                 date_from=None, date_to=None):
    """
    One party's khaata: every invoice raised and every payment made, in date
    order, with a running balance. Naam is what they owe; jama is what they
    have paid.
    """
    rows = []
    party = customer or supplier
    opening = getattr(party, 'opening_balance', None)

    if opening:
        rows.append({
            'date': opening.as_of_date.isoformat(),
            'description': 'Opening balance',
            'reference': None,
            'naam': opening.amount if opening.amount > 0 else 0,
            'jama': -opening.amount if opening.amount < 0 else 0,
        })

    if customer:
        invoices = (
            SalesInvoice.objects
            .filter(business_id=business_id, customer_id=customer.id)
            .exclude(status__in=CANCELLED_SALES_STATUSES)
        )
        for invoice in invoices:
            rows.append({
                'date': local_date(invoice.date_issued).isoformat()
                if invoice.date_issued else None,
                'description': 'Sales invoice',
                'reference': invoice.invoice_number or str(invoice.id),
                'naam': round(invoice.total or 0),
                'jama': 0,
            })

        receipts = SalesReceipt.objects.filter(
            sales_invoice__business_id=business_id,
            sales_invoice__customer_id=customer.id,
        ).filter(CLEARED_RECEIPT).select_related(
            'sales_invoice', 'transaction_record')
        for receipt in receipts:
            rows.append({
                'date': receipt_date(receipt).isoformat(),
                'description': 'Payment received',
                'reference': receipt.sales_invoice.invoice_number
                or str(receipt.sales_invoice_id),
                'naam': 0,
                'jama': round(receipt.amount or 0),
            })

    if supplier:
        invoices = (
            PurchaseInvoice.objects
            .filter(business_id=business_id, supplier_id=supplier.id)
            .exclude(status__in=CANCELLED_PURCHASE_STATUSES)
        )
        for invoice in invoices:
            rows.append({
                'date': local_date(invoice.created_at).isoformat()
                if invoice.created_at else None,
                'description': 'Purchase invoice',
                'reference': invoice.invoice_number or str(invoice.id),
                'naam': round(invoice.total or 0),
                'jama': 0,
            })

        receipts = PurchaseReceipt.objects.filter(
            purchase_invoice__business_id=business_id,
            purchase_invoice__supplier_id=supplier.id,
        ).filter(CLEARED_RECEIPT).select_related(
            'purchase_invoice', 'transaction_record')
        for receipt in receipts:
            rows.append({
                'date': receipt_date(receipt).isoformat(),
                'description': 'Payment made',
                'reference': receipt.purchase_invoice.invoice_number
                or str(receipt.purchase_invoice_id),
                'naam': 0,
                'jama': round(receipt.amount or 0),
            })

    # On-account money carries no invoice. A payment settles the balance; a
    # refund is money going back out, so it lands on the other side.
    if customer:
        party_filter = {'customer_id': customer.id}
        kinds = PARTY_ON_ACCOUNT['customer']
    else:
        party_filter = {'supplier_id': supplier.id}
        kinds = PARTY_ON_ACCOUNT['supplier']

    on_account = PartyPayment.objects.filter(
        business_id=business_id, transaction__status='C',
        transaction__type__in=[kinds['payment'], kinds['refund']],
        **party_filter
    ).select_related('transaction')
    for payment in on_account:
        is_refund = payment.transaction.type == kinds['refund']
        rows.append({
            'date': payment.transaction.date.isoformat(),
            'description': 'Refund' if is_refund else 'On-account payment',
            'reference': payment.transaction.reference,
            'naam': payment.transaction.amount if is_refund else 0,
            'jama': 0 if is_refund else payment.transaction.amount,
        })

    rows = [row for row in rows if row['date']]
    rows.sort(key=lambda row: row['date'])

    # Everything before the range is carried in as one figure, so a ranged
    # statement still starts from the party's real position.
    brought_forward = 0
    if date_from:
        cutoff = date_from.isoformat()
        brought_forward = sum(
            row['naam'] - row['jama'] for row in rows if row['date'] < cutoff
        )
        rows = [row for row in rows if row['date'] >= cutoff]
    if date_to:
        rows = [row for row in rows if row['date'] <= date_to.isoformat()]

    running = brought_forward
    for row in rows:
        running += row['naam'] - row['jama']
        row['balance'] = running

    return {
        'rows': rows,
        'brought_forward': round(brought_forward),
        'closing_balance': running,
    }


# ── Roznamcha ─────────────────────────────────────────────────────────────────

def day_book(business_id, day):
    """
    The roznamcha: one day, grouped by account, with opening and closing
    balances and totals (§6.7).
    """
    previous_day = day - timedelta(days=1)
    transactions = Transaction.objects.day_book(business_id, day)

    accounts = MoneyAccount.objects.for_business(business_id).active()
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
                'type': txn.type,
                'amount': txn.amount,
                'payment_method': txn.payment_method,
                'status': txn.status,
                'reference': txn.reference,
                'notes': txn.notes,
                'money_in': inflow,
                'money_out': outflow,
            })

        opening = account.balance(previous_day)
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
        'accounts': sections,
        'total_money_in': total_in,
        'total_money_out': total_out,
    }


def daily_summary(business_id, day):
    """
    The end-of-day picture (§6.9) — what came in, what went out, what is left,
    and what is owed.
    """
    book = day_book(business_id, day)
    totals = Transaction.objects.totals_by_type(business_id, day, day)

    credit_extended = (
        SalesInvoice.objects
        .filter(business_id=business_id, date_issued__date=day)
        .exclude(status__in=CANCELLED_SALES_STATUSES)
        .aggregate(total=Sum('total'))['total'] or 0
    )
    cash_sales = totals.get('sale_payment', 0)

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
        .filter(cheque_due_date__lte=day)
    ]

    return {
        'date': day.isoformat(),
        'accounts': [
            {
                'account_name': section['account_name'],
                'opening_balance': section['opening_balance'],
                'closing_balance': section['closing_balance'],
            }
            for section in book['accounts']
        ],
        'money_in': {
            'cash_sales': cash_sales,
            'udhaar_recovered': totals.get('customer_receipt', 0),
            'other_income': (
                totals.get('other_income', 0)
                + totals.get('owner_capital', 0)
                + totals.get('loan_received', 0)
                + totals.get('purchase_return_refund', 0)
            ),
            'total': book['total_money_in'],
        },
        'money_out': {
            'expenses': totals.get('expense', 0),
            'supplier_payments': (
                totals.get('supplier_payment', 0)
                + totals.get('purchase_payment', 0)
            ),
            'salaries': totals.get('salary_payment', 0),
            'drawings': totals.get('owner_drawings', 0),
            'total': book['total_money_out'],
        },
        'credit_extended': round(max(credit_extended - cash_sales, 0)),
        'total_receivable': receivables(business_id)['total'],
        'total_payable': payables(business_id)['total'],
        'pending_cheques': cheques_due,
        'profit': profit_estimate(business_id, day, day),
    }


# ── Munafa ────────────────────────────────────────────────────────────────────

def profit_estimate(business_id, date_from, date_to):
    """
    Revenue less cost of goods sold less expenses. An estimate only — it is
    exactly as accurate as the data entered (§6.11).
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

    expenses = (
        Expense.objects
        .filter(business_id=business_id,
                created_at__date__gte=date_from,
                created_at__date__lte=date_to)
        .aggregate(total=Sum('amount'))['total'] or 0
    )

    salaries = Transaction.objects.totals_by_type(
        business_id, date_from, date_to).get('salary_payment', 0)

    return {
        'revenue': round(revenue),
        'cost_of_goods_sold': round(cogs),
        'expenses': round(expenses + salaries),
        'profit': round(revenue - cogs - expenses - salaries),
    }
