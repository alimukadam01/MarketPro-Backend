"""
A customer, supplier or product created where it is needed - from an
invoice's picker, when the name typed is not in MarketPro yet (BUGS.md #006).

Used by the invoice create screens (root/views.py) and the backlog entry
screens (backlog/work_views.py), so both make the same records the same way:

  customer / supplier  with the balance their khaata stood at, if given, as
                       its opening balance (PartyOpeningBalance)
  product              with its variants and a stock row for each, in one
                       go - a product with no stock row cannot go on an
                       invoice

Each create is one transaction, and answers with ids and names only.
"""
from django.db import transaction
from django.utils import timezone
from rest_framework import exceptions, serializers

from accounts.models import PartyOpeningBalance
from inventory.models import InventoryItem
from .models import City, Customer, Location, Supplier, Unit


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


class NewStockSerializer(serializers.Serializer):
    """A new product's stock row. Nothing is required but the row itself -
    a sale only moves stock that has a row to move."""

    location = serializers.IntegerField(required=False, allow_null=True)
    quantity = serializers.IntegerField(required=False, allow_null=True, min_value=0)
    unit_price = serializers.FloatField(required=False, allow_null=True, min_value=0)
    unit_cost = serializers.FloatField(required=False, allow_null=True, min_value=0)


class NewProductSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=256)
    unit = serializers.IntegerField()
    desc = serializers.CharField(required=False, allow_blank=True, default='')
    variants = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    # One per variant, in the same order; one for a product with no variants.
    stock = NewStockSerializer(many=True)

    def validate(self, attrs):
        wanted = max(len(attrs['variants']), 1)
        if len(attrs['stock']) != wanted:
            raise serializers.ValidationError({'stock': [f'Give a stock row for each of the {wanted} variants.']})
        return attrs


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


def create_customer(business, data):
    values = _validated(NewCustomerSerializer, data)
    if not City.objects.filter(pk=values['city']).exists():
        raise exceptions.ValidationError({'city': ['Pick a city from the list.']})
    with transaction.atomic():
        customer = Customer.objects.create(
            business=business, name=values['name'].strip(), phone=values['phone'].strip() or None,
            address=values['address'].strip() or None, city_id=values['city'])
        _opening_balance(business, values.get('opening_balance'), customer=customer)
    return customer


def create_supplier(business, data):
    values = _validated(NewSupplierSerializer, data)
    with transaction.atomic():
        supplier = Supplier.objects.create(
            business=business, name=values['name'].strip(),
            business_name=values['business_name'].strip() or None, phone=values['phone'].strip() or None)
        _opening_balance(business, values.get('opening_balance'), supplier=supplier)
    return supplier


def create_product_with_stock(business, data):
    """The product, its variants and a stock row for each. A quantity typed
    here is a count, dated today (§8), so sales and purchases dated on or
    before today do not move it again. Returns the product and its variants,
    first to last."""
    from .serializers import ProductAndVariantCreateSerializer

    values = _validated(NewProductSerializer, data)
    if not Unit.objects.filter(pk=values['unit']).exists():
        raise exceptions.ValidationError({'unit': ['Pick a unit from the list.']})
    locations = {line.get('location') for line in values['stock'] if line.get('location')}
    if locations and Location.objects.filter(pk__in=locations, business_id=business.id).count() != len(locations):
        raise exceptions.ValidationError({'stock': ['Pick a location from the list.']})
    default_location = (Location.objects.filter(business_id=business.id)
                        .order_by('-is_default', 'id').values_list('id', flat=True).first())

    with transaction.atomic():
        product_serializer = ProductAndVariantCreateSerializer(data={
            'name': values['name'].strip(), 'unit': values['unit'], 'desc': values['desc'],
            'variants': [{'attributes': v.get('attributes') or {}} for v in values['variants']],
        }, context={'business_id': business.id})
        product_serializer.is_valid(raise_exception=True)
        product = product_serializer.save()
        variants = list(product.variants.order_by('id'))
        for variant, line in zip(variants, values['stock']):
            quantity = line.get('quantity')
            InventoryItem.objects.create(
                business_id=business.id, inventory=business.inventory_glance, product=variant,
                location_id=line.get('location') or default_location,
                quantity=quantity or 0, quantity_on_hand=quantity or 0,
                unit_price=line.get('unit_price'), unit_cost=line.get('unit_cost'),
                stock_counted_at=timezone.localdate() if quantity is not None else None)
    return product, variants


def _variant_label(variant):
    # "Enamel (Black)", or just "Enamel" for a product's default variant -
    # the same label the pickers show (backlog/outputs.py variant_label).
    name = (variant.name or '').strip()
    base = variant.base.name
    return f'{base} ({name})' if name and name.lower() != 'default' else base


def product_answer(product, variants):
    return {
        'product': {'id': product.id, 'label': product.name},
        'variants': [{'id': v.id, 'label': _variant_label(v)} for v in variants],
    }
