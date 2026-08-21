from django.db import transaction as db_transaction
from django.db.models import Q
from rest_framework import serializers

from core.serializers import SimpleUserSerializer
from root.models import Customer, Supplier
from root.serializers import SimpleCustomerSerializer, SimpleSupplierSerializer
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

    class Meta:
        model = Transaction
        fields = [
            'id', 'business', 'type', 'amount', 'date', 'account',
            'transfer_account', 'payment_method', 'status', 'reference',
            'notes', 'image', 'cheque_number', 'cheque_due_date',
            'created_by', 'party_payment', 'direction', 'is_source_linked',
            'sales_receipt', 'purchase_receipt', 'expense',
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

    class Meta:
        model = Transaction
        fields = [
            'type', 'amount', 'date', 'account', 'transfer_account',
            'payment_method', 'status', 'reference', 'notes', 'image',
            'cheque_number', 'cheque_due_date', 'customer', 'supplier',
        ]

    def save(self, **kwargs):
        validated_data = dict(self.validated_data)
        customer = validated_data.pop('customer', None)
        supplier = validated_data.pop('supplier', None)

        # A cheque is pending until it clears, so it stays financially inert.
        if validated_data.get('payment_method') == 'cheque' \
                and not validated_data.get('status'):
            validated_data['status'] = 'PEN'

        business_id = self.context['business_id']

        try:
            with db_transaction.atomic():
                instance = Transaction.objects.create(
                    business_id=business_id,
                    created_by_id=self.context.get('user_id'),
                    **validated_data
                )

                if (customer or supplier) \
                        and instance.type in Transaction.PARTY_TYPES:
                    PartyPayment.objects.create(
                        business_id=business_id,
                        customer=customer,
                        supplier=supplier,
                        transaction=instance,
                    )
            return instance
        except Exception as error:
            print(error)
            return None


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

        if (customer or supplier) and instance.type in Transaction.PARTY_TYPES:
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
