from django.db import transaction as db_transaction
from django.db.models import Q
from rest_framework import serializers

from core.serializers import SimpleUserSerializer
from root.models import Customer, Expense, Supplier
from root.serializers import SimpleCustomerSerializer, SimpleSupplierSerializer
# accounts -> sales only. accounts/utils.py and accounts/signals.py already
# import sales, and nothing under sales/ imports accounts, so there is no
# cycle. Adding a sales -> accounts import would break startup here first.
from sales.models import (
    PurchaseInvoice, PurchaseReceipt, SalesInvoice, SalesReceipt
)
from sales.utils import apply_money_details, invoice_room
from .models import (
    MoneyAccount, PartyOpeningBalance, PartyPayment, Transaction
)


# ── MoneyAccount ──────────────────────────────────────────────────────────────

class SystemAccountMixin:
    """
    Flags the account the business was created with. The lookup is cached per
    serializer instance so a list does not repeat it for every row.
    """

    is_system = serializers.SerializerMethodField()

    def get_is_system(self, account):
        if not hasattr(self, '_system_account_id'):
            self._system_account_id = MoneyAccount.objects.system_account_id(
                account.business_id)
        return account.id == self._system_account_id


class SimpleMoneyAccountSerializer(SystemAccountMixin, serializers.ModelSerializer):

    balance = serializers.SerializerMethodField()

    class Meta:
        model = MoneyAccount
        fields = [
            'id', 'name', 'type', 'opening_balance',
            'is_default', 'is_active', 'is_system', 'balance',
        ]

    def get_balance(self, account):
        return account.balance()


class MoneyAccountSerializer(SystemAccountMixin, serializers.ModelSerializer):

    business = serializers.PrimaryKeyRelatedField(read_only=True)
    balance = serializers.SerializerMethodField()
    has_transactions = serializers.SerializerMethodField()

    class Meta:
        model = MoneyAccount
        fields = [
            'id', 'business', 'name', 'type', 'opening_balance',
            'opening_date', 'is_default', 'is_active', 'is_system', 'balance',
            'has_transactions', 'created_at', 'updated_at',
        ]

    def get_balance(self, account):
        return account.balance()

    def get_has_transactions(self, account):
        return account.has_transactions()


class MoneyAccountCreateSerializer(serializers.ModelSerializer):

    # is_default and is_active are not writable here — they are changed
    # through the set-default and toggle-active actions, which keep the
    # "exactly one active default" rule intact.
    class Meta:
        model = MoneyAccount
        fields = ['name', 'type', 'opening_balance']

    def save(self, **kwargs):
        business_id = self.context['business_id']
        validated_data = dict(self.validated_data)

        # The first account a business ever gets becomes its default.
        has_default = MoneyAccount.objects.filter(
            business_id=business_id, is_default=True).exists()
        if not has_default:
            validated_data['is_default'] = True

        return MoneyAccount.objects.create(
            business_id=business_id,
            **validated_data
        )


class MoneyAccountUpdateSerializer(serializers.ModelSerializer):

    # is_default and is_active are not writable here — they are changed
    # through the set-default and toggle-active actions, which keep the
    # "exactly one active default" rule intact.
    class Meta:
        model = MoneyAccount
        fields = ['name', 'type', 'opening_balance']

    def update(self, instance, validated_data):
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        return instance


# ── Transaction ───────────────────────────────────────────────────────────────

class SimplePartyPaymentSerializer(serializers.ModelSerializer):

    customer = SimpleCustomerSerializer(read_only=True)
    supplier = SimpleSupplierSerializer(read_only=True)

    class Meta:
        model = PartyPayment
        fields = ['id', 'customer', 'supplier']


class SimpleTransactionSerializer(serializers.ModelSerializer):

    account_name = serializers.CharField(source='account.name', read_only=True)
    direction = serializers.CharField(read_only=True)
    is_source_linked = serializers.BooleanField(read_only=True)

    class Meta:
        model = Transaction
        fields = [
            'id', 'type', 'amount', 'date', 'account', 'account_name',
            'payment_method', 'status', 'reference', 'direction',
            'is_source_linked', 'cheque_number', 'cheque_due_date',
            'created_at',
        ]


class TransactionSerializer(serializers.ModelSerializer):

    account = SimpleMoneyAccountSerializer(read_only=True)
    transfer_account = SimpleMoneyAccountSerializer(read_only=True)
    party_payment = SimplePartyPaymentSerializer(read_only=True)
    created_by = SimpleUserSerializer(read_only=True)
    direction = serializers.CharField(read_only=True)
    is_source_linked = serializers.BooleanField(read_only=True)
    source = serializers.SerializerMethodField()

    def get_source(self, transaction):
        """
        What produced this row, for the locked banner on the update screen.
        The three source FKs are bare ids on the wire, which cannot tell a
        user which invoice they are looking at.
        """
        receipt = transaction.sales_receipt
        if receipt is not None:
            invoice = receipt.sales_invoice
            return {
                'kind': 'sales_invoice',
                'id': invoice.id,
                'label': invoice.invoice_number or str(invoice.id),
            }

        receipt = transaction.purchase_receipt
        if receipt is not None:
            invoice = receipt.purchase_invoice
            return {
                'kind': 'purchase_invoice',
                'id': invoice.id,
                'label': invoice.invoice_number or str(invoice.id),
            }

        expense = transaction.expense
        if expense is not None:
            return {
                'kind': 'expense',
                'id': expense.id,
                'label': expense.name,
            }

        return None

    class Meta:
        model = Transaction
        fields = [
            'id', 'business', 'type', 'amount', 'date', 'account',
            'transfer_account', 'payment_method', 'status', 'reference',
            'notes', 'image', 'cheque_number', 'cheque_due_date',
            'created_by', 'party_payment', 'direction', 'is_source_linked',
            'sales_receipt', 'purchase_receipt', 'expense', 'source',
            'created_at', 'updated_at',
        ]


class BaseTransactionWriteSerializer(serializers.ModelSerializer):
    """
    Shared validation for creating and updating a transaction.
    """

    account = serializers.PrimaryKeyRelatedField(
        queryset=MoneyAccount.objects.none())
    transfer_account = serializers.PrimaryKeyRelatedField(
        queryset=MoneyAccount.objects.none(), required=False, allow_null=True)
    customer = serializers.PrimaryKeyRelatedField(
        queryset=Customer.objects.none(), required=False,
        allow_null=True, write_only=True)
    supplier = serializers.PrimaryKeyRelatedField(
        queryset=Supplier.objects.none(), required=False,
        allow_null=True, write_only=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        business_id = self.context.get('business_id')
        if business_id:
            # New money only goes to active accounts. An account already on
            # this transaction stays selectable so history remains editable.
            selectable = Q(business_id=business_id, is_active=True)

            if self.instance:
                linked = [
                    self.instance.account_id,
                    self.instance.transfer_account_id,
                ]
                linked = [pk for pk in linked if pk]
                if linked:
                    selectable |= Q(id__in=linked)

            accounts = MoneyAccount.objects.filter(selectable)
            self.fields['account'].queryset = accounts
            self.fields['transfer_account'].queryset = accounts
            self.fields['customer'].queryset = Customer.objects.filter(
                business_id=business_id)
            self.fields['supplier'].queryset = Supplier.objects.filter(
                business_id=business_id)

            # Source pickers, present only on the create serializer. Scoped
            # here so a foreign business's invoice is a 400, not a leak.
            # Each model's own CANCELLED_STATUSES, never a literal: 'C' means
            # COMPLETED on a sales invoice and CANCELLED on a purchase one.
            if 'sales_invoice' in self.fields:
                self.fields['sales_invoice'].queryset = (
                    SalesInvoice.objects
                    .filter(business_id=business_id)
                    .exclude(status__in=SalesInvoice.CANCELLED_STATUSES)
                )
            if 'purchase_invoice' in self.fields:
                self.fields['purchase_invoice'].queryset = (
                    PurchaseInvoice.objects
                    .filter(business_id=business_id)
                    .exclude(status__in=PurchaseInvoice.CANCELLED_STATUSES)
                )

    def validate(self, attrs):
        txn_type = attrs.get('type') or getattr(self.instance, 'type', None)
        amount = attrs.get('amount', getattr(self.instance, 'amount', 0))
        method = attrs.get(
            'payment_method', getattr(self.instance, 'payment_method', 'cash'))

        # Whole rupees only; negatives are meaningful for adjustments alone.
        if txn_type != 'cash_adjustment' and amount is not None and amount <= 0:
            raise serializers.ValidationError({
                'amount': 'Amount must be greater than zero.'
            })

        if txn_type == 'cash_adjustment' and amount == 0:
            raise serializers.ValidationError({
                'amount': 'A cash adjustment cannot be zero.'
            })

        if txn_type == 'transfer':
            destination = attrs.get(
                'transfer_account',
                getattr(self.instance, 'transfer_account', None))
            source = attrs.get(
                'account', getattr(self.instance, 'account', None))

            if not destination:
                raise serializers.ValidationError({
                    'transfer_account': 'A transfer needs a destination account.'
                })
            if source and destination and source.id == destination.id:
                raise serializers.ValidationError({
                    'transfer_account':
                        'Destination must differ from the source account.'
                })

        if method == 'cheque':
            cheque_number = attrs.get(
                'cheque_number', getattr(self.instance, 'cheque_number', None))
            if not cheque_number:
                raise serializers.ValidationError({
                    'cheque_number': 'A cheque payment needs a cheque number.'
                })

        customer = attrs.get('customer')
        supplier = attrs.get('supplier')
        if customer and supplier:
            raise serializers.ValidationError({
                'customer': 'Name either a customer or a supplier, not both.'
            })

        return attrs


class TransactionCreateSerializer(BaseTransactionWriteSerializer):
    """
    Creates a transaction, and for the three source-backed types creates the
    record that owns it instead.

    A sale payment IS a receipt against an invoice; an expense IS a row on the
    expenses ledger. Writing a bare Transaction for either leaves the books
    disagreeing with themselves - an invoice-less sale payment is counted by
    daily_summary but invisible to the party ledger, and an Expense-less
    expense transaction is invisible to profit_estimate and to expense targets.
    So those branches create the source and let the existing signal mint the
    transaction, then patch the money details onto it.
    """

    # Source pickers. Write-only and not model fields; querysets are scoped in
    # BaseTransactionWriteSerializer.__init__.
    sales_invoice = serializers.PrimaryKeyRelatedField(
        queryset=SalesInvoice.objects.none(), required=False,
        allow_null=True, write_only=True)
    purchase_invoice = serializers.PrimaryKeyRelatedField(
        queryset=PurchaseInvoice.objects.none(), required=False,
        allow_null=True, write_only=True)
    expense_name = serializers.CharField(
        max_length=256, required=False, allow_null=True,
        allow_blank=True, write_only=True)
    expense_category = serializers.ChoiceField(
        choices=Expense.CATEGORY_CHOICES, required=False,
        allow_null=True, allow_blank=True, write_only=True)

    # Which type is backed by which source. The party types are absent on
    # purpose: they are on-account money with no invoice behind them.
    SOURCE_TYPES = {
        'sale_payment': 'sales_invoice',
        'purchase_payment': 'purchase_invoice',
        'expense': 'expense_name',
    }

    class Meta:
        model = Transaction
        fields = [
            'type', 'amount', 'date', 'account', 'transfer_account',
            'payment_method', 'status', 'reference', 'notes', 'image',
            'cheque_number', 'cheque_due_date', 'customer', 'supplier',
            'sales_invoice', 'purchase_invoice',
            'expense_name', 'expense_category',
        ]

    def validate(self, attrs):
        attrs = super().validate(attrs)

        txn_type = attrs.get('type')
        amount = float(attrs.get('amount') or 0)

        # A sale or purchase payment settles an invoice, so it must name one.
        # Without this the screen can manufacture revenue that no customer is
        # ever credited for: daily_summary counts it, party_ledger does not.
        # On-account money belongs to customer_receipt / supplier_payment.
        if txn_type == 'sale_payment' and not attrs.get('sales_invoice'):
            raise serializers.ValidationError({
                'sales_invoice':
                    'A sale payment settles an invoice, so it must name one. '
                    'For money on account, use Customer Payment.'
            })

        if txn_type == 'purchase_payment' and not attrs.get('purchase_invoice'):
            raise serializers.ValidationError({
                'purchase_invoice':
                    'A purchase payment settles an invoice, so it must name '
                    'one. For money on account, use Supplier Payment.'
            })

        if txn_type == 'expense' and not (attrs.get('expense_name') or '').strip():
            raise serializers.ValidationError({
                'expense_name': 'An expense needs a name.'
            })

        # Same room rule the payments dialog uses, from the same helper, so the
        # two screens can never disagree about what an invoice can still take.
        invoice = attrs.get('sales_invoice') or attrs.get('purchase_invoice')
        if invoice is not None and amount > invoice_room(invoice):
            raise serializers.ValidationError({
                'amount': 'accumulated amount cannot exceed invoice total'
            })

        return attrs

    def create(self, validated_data):
        """
        create(), not save().

        This used to override save() and wrap the whole write in
        `except Exception: print(error); return None`. Two things followed from
        that, and together they were a silent data-loss bug:

        1. Overriding save() meant self.instance was never assigned, because it
           is ModelSerializer.save() that does `self.instance = self.create(...)`.
        2. With self.instance still None and no errors recorded, DRF's
           Serializer.data falls back to to_representation(self.validated_data)
           - it renders the SUBMITTED PAYLOAD back. CreateModelMixin then
           answered 201 Created for a write that never happened.

        So any failure at all - an unwritable MEDIA_ROOT on the image, a
        constraint, anything - was reported to the user as success. Letting the
        exception propagate is the fix; DRF turns it into a 500, and a 500 is
        the truth.
        """
        data = dict(validated_data)
        sales_invoice = data.pop('sales_invoice', None)
        purchase_invoice = data.pop('purchase_invoice', None)
        expense_name = (data.pop('expense_name', None) or '').strip()
        expense_category = data.pop('expense_category', None) or None

        txn_type = data.get('type')

        # Exactly one branch runs, and every branch returns a Transaction.
        # Writing both a plain transaction and a source record would double
        # every figure it touches - balance(), money_in, daily_summary.
        with db_transaction.atomic():
            if txn_type == 'sale_payment' and sales_invoice is not None:
                return self._via_receipt(
                    SalesReceipt, {'sales_invoice': sales_invoice}, data)

            if txn_type == 'purchase_payment' and purchase_invoice is not None:
                return self._via_receipt(
                    PurchaseReceipt,
                    {'purchase_invoice': purchase_invoice}, data)

            if txn_type == 'expense' and expense_name:
                return self._via_expense(expense_name, expense_category, data)

            return self._plain(data)

    # ── branches ─────────────────────────────────────────────────────────────

    def _money_details(self, data):
        """
        What the signal does not know: which account the money actually moved
        through, how, when, and the photo of the slip.

        status is deliberately absent. The signal defaults it to cleared, and
        apply_money_details flips it to PEN for a cheque - letting this pass a
        status too would give one field two owners.

        amount is absent for the same reason: the signal takes it from the
        source record, so writing it here could let the two drift apart.
        """
        details = {
            'account_id': data['account'].id,
            'payment_method': data.get('payment_method') or 'cash',
            'created_by_id': self.context.get('user_id'),
        }
        for field in ('date', 'cheque_number', 'cheque_due_date',
                      'reference', 'image'):
            value = data.get(field)
            if value:
                details[field] = value
        return details

    def _via_receipt(self, model, link, data):
        """
        The money is a payment against an invoice, so record the payment and
        let the existing signal mirror it. This is the same path the invoice
        screen uses, which is the point: the invoice's paid amount and status
        move, and the party ledger sees it.
        """
        receipt = model.objects.create(
            amount=data.get('amount') or 0,
            # The signal copies desc onto the transaction's notes, so notes
            # travel as the receipt's desc rather than as a later patch -
            # otherwise the payments dialog row would render blank.
            desc=data.get('notes'),
            **link
        )
        # refresh_from_db so transaction_record is resolved after the signal.
        receipt.refresh_from_db()
        return apply_money_details(receipt, self._money_details(data))

    def _via_expense(self, name, category, data):
        """
        An expense transaction with no Expense row is invisible to
        profit_estimate and to every expense target, while still being counted
        by the day book. Minting the Expense keeps the two in step.
        """
        expense = Expense.objects.create(
            business_id=self.context['business_id'],
            name=name,
            category=category,
            desc=data.get('notes'),
            amount=data.get('amount') or 0,
        )
        expense.refresh_from_db()
        return apply_money_details(expense, self._money_details(data))

    def _plain(self, data):
        """
        Everything with no source record behind it: the party types, transfers,
        adjustments, capital, loans, salaries, drawings, other income/payment.
        """
        customer = data.pop('customer', None)
        supplier = data.pop('supplier', None)

        # A cheque is pending until it clears, so it stays financially inert.
        if data.get('payment_method') == 'cheque' and not data.get('status'):
            data['status'] = 'PEN'

        business_id = self.context['business_id']

        instance = Transaction.objects.create(
            business_id=business_id,
            created_by_id=self.context.get('user_id'),
            **data
        )

        # Only PARTY_TYPES get a PartyPayment. sale_payment and
        # purchase_payment must NEVER be added to that list: customer_balance
        # sums invoice receipts and on-account payments independently, so a row
        # in both would credit the party twice for the same money.
        if (customer or supplier) and instance.type in Transaction.PARTY_TYPES:
            PartyPayment.objects.create(
                business_id=business_id,
                customer=customer,
                supplier=supplier,
                transaction=instance,
            )

        return instance


class TransactionUpdateSerializer(BaseTransactionWriteSerializer):

    class Meta:
        model = Transaction
        fields = [
            'type', 'amount', 'date', 'account', 'transfer_account',
            'payment_method', 'status', 'reference', 'notes', 'image',
            'cheque_number', 'cheque_due_date', 'customer', 'supplier',
        ]
        extra_kwargs = {'image': {'required': False}}

    def update(self, instance, validated_data):
        customer = validated_data.pop('customer', None)
        supplier = validated_data.pop('supplier', None)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        if instance.type not in Transaction.PARTY_TYPES:
            # Changing to a type that names no party used to leave the old
            # PartyPayment behind, because the branch below only ever ran when
            # a party was supplied - there was no way to clear one.
            PartyPayment.objects.filter(transaction=instance).delete()
        elif customer or supplier:
            PartyPayment.objects.update_or_create(
                transaction=instance,
                defaults={
                    'business_id': instance.business_id,
                    'customer': customer,
                    'supplier': supplier,
                },
            )

        return instance


# ── PartyOpeningBalance ───────────────────────────────────────────────────────

class PartyOpeningBalanceSerializer(serializers.ModelSerializer):

    business = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = PartyOpeningBalance
        fields = [
            'id', 'business', 'customer', 'supplier',
            'amount', 'as_of_date', 'created_at',
        ]

    def validate(self, attrs):
        customer = attrs.get('customer')
        supplier = attrs.get('supplier')

        if not customer and not supplier:
            raise serializers.ValidationError({
                'customer': 'Name either a customer or a supplier.'
            })
        if customer and supplier:
            raise serializers.ValidationError({
                'customer': 'Name either a customer or a supplier, not both.'
            })
        return attrs

    def create(self, validated_data):
        return PartyOpeningBalance.objects.create(
            business_id=self.context['business_id'],
            **validated_data
        )

    def update(self, instance, validated_data):
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        return instance
