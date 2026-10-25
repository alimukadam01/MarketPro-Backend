"""
A customer or supplier created where it is needed - from an invoice's
picker, when the name typed is not in MarketPro yet (BUGS.md #006).

Used by the invoice screens (root/views.py) and the backlog entry screens
(backlog/work_views.py), so both make the same records the same way. On the
invoice screens the balance their khaata stood at can come with them, as
the opening balance (PartyOpeningBalance); the backlog's do not take one -
a money figure typed while entering an old bill is a guess.

Products are not created here. A product created to finish an invoice came
with a stock figure made up to let the sale through, and stock that grows
wrong with every such invoice; products and their stock are made on the
Products and Inventory screens, from a real catalogue or count.

Each create is one transaction, and answers with ids and names only.
"""
from django.db import transaction
from rest_framework import exceptions, serializers

from accounts.models import PartyOpeningBalance
from .models import City, Customer, Supplier


class OpeningBalanceSerializer(serializers.Serializer):
    """A balance carried over from the paper khaata. Positive: the customer
    owes the shop, or the shop owes the supplier (PartyOpeningBalance)."""

    amount = serializers.IntegerField()
    as_of_date = serializers.DateField()


class NewCustomerSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=256)
    phone = serializers.CharField(max_length=256, required=False, allow_blank=True, default='')
    address = serializers.CharField(required=False, allow_blank=True, default='')
    city = serializers.IntegerField()
    opening_balance = OpeningBalanceSerializer(required=False, allow_null=True)


class NewSupplierSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=256)
    business_name = serializers.CharField(max_length=256, required=False, allow_blank=True, default='')
    phone = serializers.CharField(max_length=256, required=False, allow_blank=True, default='')
    opening_balance = OpeningBalanceSerializer(required=False, allow_null=True)


def _validated(serializer_class, data):
    serializer = serializer_class(data=data)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


def _opening_balance(business, values, **party):
    if values and values.get('amount'):
        PartyOpeningBalance.objects.create(
            business=business, amount=values['amount'], as_of_date=values['as_of_date'], **party)


def supplier_label(supplier):
    return f'{supplier.name} ({supplier.business_name})' if supplier.business_name else supplier.name


def create_customer(business, data, with_balance=True):
    """with_balance=False (the backlog): any opening balance sent is ignored."""
    values = _validated(NewCustomerSerializer, data)
    if not City.objects.filter(pk=values['city']).exists():
        raise exceptions.ValidationError({'city': ['Pick a city from the list.']})
    with transaction.atomic():
        customer = Customer.objects.create(
            business=business, name=values['name'].strip(), phone=values['phone'].strip() or None,
            address=values['address'].strip() or None, city_id=values['city'])
        if with_balance:
            _opening_balance(business, values.get('opening_balance'), customer=customer)
    return customer


def create_supplier(business, data, with_balance=True):
    """with_balance=False (the backlog): any opening balance sent is ignored."""
    values = _validated(NewSupplierSerializer, data)
    with transaction.atomic():
        supplier = Supplier.objects.create(
            business=business, name=values['name'].strip(),
            business_name=values['business_name'].strip() or None, phone=values['phone'].strip() or None)
        if with_balance:
            _opening_balance(business, values.get('opening_balance'), supplier=supplier)
    return supplier
