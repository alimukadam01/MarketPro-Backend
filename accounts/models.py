from calendar import monthrange
from datetime import date as date_cls

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from root.models import Business, Customer, Expense, Supplier


class MoneyAccountQuerySet(models.QuerySet):

    def for_business(self, business_id):
        return self.filter(business_id=business_id)

    def active(self):
        return self.filter(is_active=True)


class MoneyAccountManager(models.Manager):

    def get_queryset(self):
        return MoneyAccountQuerySet(self.model, using=self._db)

    def for_business(self, business_id):
        return self.get_queryset().for_business(business_id)

    def active(self):
        return self.get_queryset().active()

    def system_account_id(self, business_id):
        """
        The account the business was created with — the one the signal makes.
        It is permanent: it can be neither deactivated nor deleted, so the
        business always has somewhere for money to land. The user may move the
        default onto any other account, but it comes back here if that one is
        ever deactivated.
        """
        return (
            self.get_queryset()
            .for_business(business_id)
            .order_by('id')
            .values_list('id', flat=True)
            .first()
        )

    def get_default_account(self, business_id):
        """
        The account new money lands in. Every business is given one when it is
        created, and it can neither be deactivated nor unset, so this normally
        just reads it back. The create below only covers businesses that
        predate this module and therefore never got one.
        """
        account = (
            self.get_queryset()
            .for_business(business_id)
            .active()
            .filter(is_default=True)
            .first()
        )
        if account:
            return account

        account, _ = self.get_queryset().get_or_create(
            business_id=business_id,
            type='cash',
            is_default=True,
            is_active=True,
            defaults={'name': 'Cash', 'opening_balance': 0},
        )
        return account

    def cash_in_hand(self, business_id, as_of=None):
        """
        Per-account balances plus the total across every active account.
        """
        accounts = self.get_queryset().for_business(business_id).active()

        rows = []
        total = 0
        for account in accounts:
            balance = account.balance(as_of)
            total += balance
            rows.append({
                'id': account.id,
                'name': account.name,
                'type': account.type,
                'balance': balance,
            })

        return {'accounts': rows, 'total': total}


class MoneyAccount(models.Model):

    TYPE_CHOICES = [
        ('cash', 'Cash'),
        ('wallet', 'Mobile Wallet'),
        ('bank', 'Bank'),
    ]

    business = models.ForeignKey(
        Business, on_delete=models.CASCADE, related_name='money_accounts')
    name = models.CharField(max_length=256)
    type = models.CharField(
        max_length=256, choices=TYPE_CHOICES, default='cash')
    opening_balance = models.IntegerField(default=0)
    # auto_now_add would use the server's OS date rather than TIME_ZONE.
    opening_date = models.DateField(default=timezone.localdate)
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = MoneyAccountManager()

    def __str__(self):
        return f"{self.name}-{self.type}"

    def balance(self, as_of=None):
        """
        Opening balance plus every cleared movement touching this account.
        Never stored — always computed on read (BRD rule 3).
        """
        outgoing = self.transactions.filter(status='C')
        incoming = self.incoming_transfers.filter(status='C')

        if as_of:
            outgoing = outgoing.filter(date__lte=as_of)
            incoming = incoming.filter(date__lte=as_of)

        money_in = outgoing.filter(
            type__in=Transaction.IN_TYPES
        ).aggregate(total=models.Sum('amount'))['total'] or 0

        money_out = outgoing.filter(
            type__in=Transaction.OUT_TYPES
        ).aggregate(total=models.Sum('amount'))['total'] or 0

        # Transfers leave this account when it is the source.
        transferred_out = outgoing.filter(
            type='transfer'
        ).aggregate(total=models.Sum('amount'))['total'] or 0

        # ... and enter it when it is the destination.
        transferred_in = incoming.filter(
            type='transfer'
        ).aggregate(total=models.Sum('amount'))['total'] or 0

        # Adjustments are signed — they may be positive or negative.
        adjustments = outgoing.filter(
            type='cash_adjustment'
        ).aggregate(total=models.Sum('amount'))['total'] or 0

        return (
            self.opening_balance
            + money_in - money_out
            - transferred_out + transferred_in
            + adjustments
        )

    def has_transactions(self):
        return self.transactions.exists() or self.incoming_transfers.exists()

    @property
    def is_system(self):
        """The account created with the business. Permanent."""
        return self.id == MoneyAccount.objects.system_account_id(
            self.business_id)


class TransactionQuerySet(models.QuerySet):

    def for_business(self, business_id):
        return self.filter(business_id=business_id)

    def cleared(self):
        return self.filter(status='C')

    def in_period(self, date_from, date_to):
        return self.filter(date__gte=date_from, date__lte=date_to)

    ### The two below duplicate root.BaseQuerySet.in_range and filter_optional.
    ### They are copied rather than inherited because this queryset already has
    ### an in_period(date_from, date_to), and BaseQuerySet's in_period takes a
    ### number of days — rebasing would silently change what every existing
    ### caller of this one means.

    def in_range(self, date_lookup, date_from, date_to):
        return self.filter(**{
            f'{date_lookup}__gte': date_from,
            f'{date_lookup}__lte': date_to,
        })

    def filter_optional(self, **kwargs):
        return self.filter(**{
            key: value for key, value in kwargs.items() if value is not None
        })


class TransactionManager(models.Manager):

    def get_queryset(self):
        return TransactionQuerySet(self.model, using=self._db)

    def for_business(self, business_id):
        return self.get_queryset().for_business(business_id)

    def cleared(self):
        return self.get_queryset().cleared()

    def totals_by_type(self, business_id, date_from, date_to):
        """
        Cleared amount per transaction type over a period.
        Returns { type: amount } — callers pick the lines they need.
        """
        rows = (
            self.get_queryset()
            .for_business(business_id)
            .cleared()
            .in_period(date_from, date_to)
            .values('type')
            .annotate(total=models.Sum('amount'))
        )
        return {row['type']: row['total'] or 0 for row in rows}

    def salary_expense_total(self, business_id, date_from, date_to):
        """
        Cleared money paid out as salaries, which are recorded as an expense in
        the salary category rather than as their own transaction type.

        The daily summary reports salaries on their own line, so it needs to
        move this amount out of the expenses line and into the salaries one.
        Amounts come from the transaction, not the Expense row, so this stays
        comparable with totals_by_type and is dated the same way.
        """
        return (
            self.get_queryset()
            .for_business(business_id)
            .cleared()
            .in_period(date_from, date_to)
            .filter(type='expense', expense__category=Expense.SALARY_CATEGORY)
            .aggregate(total=models.Sum('amount'))['total'] or 0
        )

    def money_in(self, business_id, date_from, date_to):
        return (
            self.get_queryset()
            .for_business(business_id)
            .cleared()
            .in_period(date_from, date_to)
            .filter(type__in=Transaction.IN_TYPES)
            .aggregate(total=models.Sum('amount'))['total'] or 0
        )

    def money_out(self, business_id, date_from, date_to):
        return (
            self.get_queryset()
            .for_business(business_id)
            .cleared()
            .in_period(date_from, date_to)
            .filter(type__in=Transaction.OUT_TYPES)
            .aggregate(total=models.Sum('amount'))['total'] or 0
        )

    def money_for_types(self, business_id, types, date_from=None, date_to=None):
        """
        Cleared money for a set of transaction types, optionally bounded by
        the money-movement date. Powers the sales / purchase KPIs.
        """
        queryset = (
            self.get_queryset()
            .for_business(business_id)
            .cleared()
            .filter(type__in=types)
        )

        if date_from:
            queryset = queryset.filter(date__gte=date_from)
        if date_to:
            queryset = queryset.filter(date__lte=date_to)

        return queryset.aggregate(total=models.Sum('amount'))['total'] or 0

    def _party_money(self, business_id, date_from, date_to, in_types,
                     refund_type, party_filter=None, payment_method=None):
        """
        Cleared money for a set of types, less the refunds that reverse it.

        One query, two conditional sums. Coalesce guards both legs because a
        Sum over no rows is NULL, and NULL minus 0 is NULL rather than 0 — a
        period with only a refund in it has to come back negative, not empty.

        status='C' is the point of .cleared(): a pending or bounced cheque is
        recorded against the party but has settled nothing.

        Transaction.date is a DateField, so in_range takes the bare field name.
        '__date' on a DateField raises FieldError.
        """
        queryset = (
            self.get_queryset()
            .for_business(business_id)
            .cleared()
            .in_range('date', date_from, date_to)
            .filter_optional(payment_method=payment_method)
        )

        if party_filter is not None:
            queryset = queryset.filter(party_filter)

        return queryset.aggregate(value=(
            Coalesce(models.Sum('amount', filter=Q(type__in=in_types)), 0)
            - Coalesce(models.Sum('amount', filter=Q(type=refund_type)), 0)
        ))['value'] or 0

    def cash_received(self, business_id, date_from, date_to,
                      customer_id=None, payment_method=None):
        """
        Cleared money in from sales, less refunds paid back to customers.

        Not the same number as SalesInvoice.objects.revenue: this is money that
        moved, dated on the movement, while revenue is invoiced value dated on
        the invoice. A credit sale raises revenue immediately and this not at
        all until it is paid.
        """
        party_filter = None
        if customer_id is not None:
            # A sale_payment reaches the customer through the receipt and its
            # invoice; a customer_receipt or a refund reaches it through
            # PartyPayment. Both paths have to be searched.
            party_filter = (
                Q(sales_receipt__sales_invoice__customer_id=customer_id)
                | Q(party_payment__customer_id=customer_id)
            )

        return self._party_money(
            business_id, date_from, date_to,
            in_types=Transaction.SALES_MONEY_TYPES,
            refund_type='sales_return_refund',
            party_filter=party_filter,
            payment_method=payment_method,
        )

    def cash_paid(self, business_id, date_from, date_to,
                  supplier_id=None, payment_method=None):
        """
        Cleared money out for purchases, less refunds received back from
        suppliers. The mirror of cash_received.
        """
        party_filter = None
        if supplier_id is not None:
            party_filter = (
                Q(purchase_receipt__purchase_invoice__supplier_id=supplier_id)
                | Q(party_payment__supplier_id=supplier_id)
            )

        return self._party_money(
            business_id, date_from, date_to,
            in_types=Transaction.PURCHASE_MONEY_TYPES,
            refund_type='purchase_return_refund',
            party_filter=party_filter,
            payment_method=payment_method,
        )

    def monthly_type_trend(self, business_id, types):
        """
        Day-by-day cleared money for a set of types over the current month.
        Same {day, value} shape as BaseQuerySet.monthly_trend.
        """
        today = timezone.localdate()
        year, month = today.year, today.month
        _, days_in_month = monthrange(year, month)

        rows = (
            self.get_queryset()
            .for_business(business_id)
            .cleared()
            .filter(type__in=types, date__year=year, date__month=month)
            .values('date')
            .annotate(total=models.Sum('amount'))
        )
        trend_map = {row['date']: row['total'] or 0 for row in rows}

        result = []
        for day_num in range(1, days_in_month + 1):
            current_date = date_cls(year, month, day_num)
            value = 0.0 if current_date > today else float(
                trend_map.get(current_date, 0))
            result.append({'day': current_date.isoformat(), 'value': value})

        return result

    def day_book(self, business_id, day):
        return (
            self.get_queryset()
            .for_business(business_id)
            .filter(date=day)
            .select_related('account', 'transfer_account')
            .order_by('created_at')
        )

    def pending_cheques(self, business_id):
        """
        Every pending cheque transaction. Stays a queryset so callers can
        narrow it further - daily_summary filters it by due date.
        """
        return (
            self.get_queryset()
            .for_business(business_id)
            .filter(payment_method='cheque', status='PEN')
            .order_by('cheque_due_date')
        )

    def pending_cheques_grouped(self, business_id):
        """
        One row per physical CHEQUE, not per transaction.

        A payment allocated across several items writes one transaction per
        item, all sharing a payment_group and one cheque. The cheque is what
        the user clears or bounces, so a group collapses to a single row
        carrying the cheque's full face value - otherwise one cheque appears
        as three entries needing three clicks.

        Returns a list. The representative's amount is summed in memory for
        display and is never saved.
        """
        rows = (
            self.pending_cheques(business_id)
            .select_related('account')
            .order_by('cheque_due_date', 'id')
        )

        collapsed = []
        representatives = {}

        for transaction in rows:
            group = transaction.payment_group
            if group is None:
                collapsed.append(transaction)
                continue

            first = representatives.get(group)
            if first is None:
                representatives[group] = transaction
                collapsed.append(transaction)
            else:
                first.amount = (first.amount or 0) + (transaction.amount or 0)

        return collapsed

    def monthly_cash_trend(self, business_id):
        """
        Day-by-day net cash movement for the current month.
        Mirrors BaseQuerySet.monthly_trend, but over `date` and signed.
        """
        today = timezone.localdate()
        year, month = today.year, today.month
        _, days_in_month = monthrange(year, month)

        rows = (
            self.get_queryset()
            .for_business(business_id)
            .cleared()
            .filter(date__year=year, date__month=month)
            .values('date', 'type')
            .annotate(total=models.Sum('amount'))
        )

        net_map = {}
        for row in rows:
            amount = row['total'] or 0
            if row['type'] in Transaction.IN_TYPES:
                net_map[row['date']] = net_map.get(row['date'], 0) + amount
            elif row['type'] in Transaction.OUT_TYPES:
                net_map[row['date']] = net_map.get(row['date'], 0) - amount
            elif row['type'] == 'cash_adjustment':
                net_map[row['date']] = net_map.get(row['date'], 0) + amount

        result = []
        for day_num in range(1, days_in_month + 1):
            current_date = date_cls(year, month, day_num)
            value = 0.0 if current_date > today else float(
                net_map.get(current_date, 0))
            result.append({'day': current_date.isoformat(), 'value': value})

        return result


class Transaction(models.Model):
    """
    A transaction exists if and only if money actually moved (BRD §5).
    Credit sales and credit purchases are not transactions — they are invoices.
    """

    IN_TYPES = [
        'sale_payment',
        'customer_receipt',
        'purchase_return_refund',
        'owner_capital',
        'loan_received',
        'other_income',
    ]

    OUT_TYPES = [
        'purchase_payment',
        'supplier_payment',
        'sales_return_refund',
        'expense',
        'salary_payment',
        'owner_drawings',
        'loan_repayment',
        'other_payment',
    ]

    NEUTRAL_TYPES = [
        'transfer',
        'cash_adjustment',
    ]

    # Money that counts toward sales / purchase KPIs (every rupee actually
    # received from customers, and every rupee actually paid to suppliers).
    SALES_MONEY_TYPES = ['sale_payment', 'customer_receipt']
    PURCHASE_MONEY_TYPES = ['purchase_payment', 'supplier_payment']

    # Types that name a customer or supplier when recorded on account.
    PARTY_TYPES = [
        'customer_receipt',
        'supplier_payment',
        'sales_return_refund',
        'purchase_return_refund',
    ]

    TYPE_CHOICES = [
        ('sale_payment', 'Sale Payment'),
        # Label only. The code stays customer_receipt so existing rows,
        # filters and PARTY_ON_ACCOUNT keep working; renaming it would
        # need a data migration for no user-visible gain.
        ('customer_receipt', 'Customer Payment'),
        ('purchase_return_refund', 'Purchase Return Refund'),
        ('owner_capital', 'Owner Capital'),
        ('loan_received', 'Loan Received'),
        ('other_income', 'Other Income'),
        ('purchase_payment', 'Purchase Payment'),
        ('supplier_payment', 'Supplier Payment'),
        ('sales_return_refund', 'Sales Return Refund'),
        ('expense', 'Expense'),
        ('salary_payment', 'Salary Payment'),
        ('owner_drawings', 'Owner Drawings'),
        ('loan_repayment', 'Loan Repayment'),
        ('other_payment', 'Other Payment'),
        ('transfer', 'Account Transfer'),
        ('cash_adjustment', 'Cash Adjustment'),
    ]

    PAYMENT_METHOD_CHOICES = [
        ('cash', 'Cash'),
        ('wallet', 'Wallet'),
        ('bank_transfer', 'Bank Transfer'),
        ('cheque', 'Cheque'),
    ]

    STATUS_CHOICES = [
        ('C', 'CLEARED'),
        ('PEN', 'PENDING'),
        ('B', 'BOUNCED'),
    ]

    business = models.ForeignKey(
        Business, on_delete=models.CASCADE, related_name='transactions')
    account = models.ForeignKey(
        MoneyAccount, on_delete=models.CASCADE, related_name='transactions')
    transfer_account = models.ForeignKey(
        MoneyAccount, on_delete=models.CASCADE,
        null=True, blank=True, related_name='incoming_transfers',
        help_text='Destination account when type is transfer.'
    )
    type = models.CharField(max_length=256, choices=TYPE_CHOICES)
    amount = models.IntegerField(
        default=0,
        help_text='Whole rupees. Negative only permitted for cash_adjustment.'
    )
    date = models.DateField(
        help_text='The date the money moved. Backdating is unrestricted.')
    payment_method = models.CharField(
        max_length=256, choices=PAYMENT_METHOD_CHOICES, default='cash')
    status = models.CharField(
        max_length=256, choices=STATUS_CHOICES, default='C')
    reference = models.CharField(max_length=256, null=True, blank=True)
    notes = models.TextField(null=True, blank=True)
    image = models.ImageField(upload_to='transactions', null=True, blank=True)
    cheque_number = models.CharField(max_length=256, null=True, blank=True)
    cheque_due_date = models.DateField(null=True, blank=True)
    payment_group = models.UUIDField(
        null=True, blank=True, db_index=True,
        help_text='Set when one payment was allocated across several items. '
                  'Every transaction in a group is one physical payment, so a '
                  'cheque clears or bounces for the whole group at once.'
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='created_transactions'
    )

    # Source links. The transaction is the money record; these carry the
    # context. CASCADE means deleting the source removes the transaction.
    sales_receipt = models.OneToOneField(
        'sales.SalesReceipt', on_delete=models.CASCADE,
        null=True, blank=True, related_name='transaction_record'
    )
    purchase_receipt = models.OneToOneField(
        'sales.PurchaseReceipt', on_delete=models.CASCADE,
        null=True, blank=True, related_name='transaction_record'
    )
    expense = models.OneToOneField(
        'root.Expense', on_delete=models.CASCADE,
        null=True, blank=True, related_name='transaction_record'
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TransactionManager()

    class Meta:
        ordering = ['-date', '-created_at']

    def __str__(self):
        return f"{self.id}-{self.type}-{self.amount}-{self.status}"

    @property
    def direction(self):
        if self.type in self.IN_TYPES:
            return 'in'
        if self.type in self.OUT_TYPES:
            return 'out'
        return 'neutral'

    @property
    def is_source_linked(self):
        """
        True when the transaction mirrors a receipt or expense — such a record
        is edited from its source, not directly.
        """
        return bool(
            self.sales_receipt_id
            or self.purchase_receipt_id
            or self.expense_id
        )


class PartyPayment(models.Model):
    """
    Party context for an on-account payment. The transaction knows only the
    money; who it was with lives here (BRD §5).
    """

    business = models.ForeignKey(
        Business, on_delete=models.CASCADE, related_name='party_payments')
    customer = models.ForeignKey(
        Customer, on_delete=models.CASCADE,
        null=True, blank=True, related_name='party_payments'
    )
    supplier = models.ForeignKey(
        Supplier, on_delete=models.CASCADE,
        null=True, blank=True, related_name='party_payments'
    )
    transaction = models.OneToOneField(
        Transaction, on_delete=models.CASCADE, related_name='party_payment')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        party = self.customer or self.supplier
        return f"{self.id}-{party}-{self.transaction.amount}"


class PartyOpeningBalance(models.Model):
    """
    A balance carried across from the paper khaata at onboarding (BRD §6.6).
    Counted in party balances, excluded from period reporting.
    """

    business = models.ForeignKey(
        Business, on_delete=models.CASCADE,
        related_name='party_opening_balances'
    )
    customer = models.OneToOneField(
        Customer, on_delete=models.CASCADE,
        null=True, blank=True, related_name='opening_balance'
    )
    supplier = models.OneToOneField(
        Supplier, on_delete=models.CASCADE,
        null=True, blank=True, related_name='opening_balance'
    )
    amount = models.IntegerField(
        default=0,
        help_text='Positive means the customer owes us, or we owe the supplier.'
    )
    as_of_date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        party = self.customer or self.supplier
        return f"{self.id}-{party}-{self.amount}"
