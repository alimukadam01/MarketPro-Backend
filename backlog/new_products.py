"""
A product, or new variants of one, created from a backlog stock page when
the page counts something MarketPro does not have (BUGS.md #006).

Only a stock page. There the page itself is the count: the product is made
without stock, and the stock line the entry person enters next makes its
stock row, with the quantity and the day it was counted. An invoice picker
never creates a product (root/new_records.py) - one made to finish an
invoice came with a stock figure invented to let the sale through.

Names and attributes only, in and out: never a price or a quantity.

  new product       name, unit, description and its variants (without any,
                    it has one, as on Create Product)
  existing product  new variants only - its details and the variants it has
                    stay as they are; those are changed on Update Product
"""
from django.db import transaction
from rest_framework import exceptions, serializers

from audit.helpers import audited_bulk_create
from root.models import Product, ProductVariant, Unit
from root.serializers import ProductAndVariantCreateSerializer
from root.utils import generate_sku, generate_variant_name
from .outputs import variant_label

MAX_ATTRIBUTES = 3


class VariantSerializer(serializers.Serializer):
    attributes = serializers.DictField(child=serializers.CharField(allow_blank=True), required=False, default=dict)

    def validate_attributes(self, attributes):
        attributes = {str(key).strip(): str(value).strip() for key, value in attributes.items()}
        if not attributes or len(attributes) > MAX_ATTRIBUTES or not all(attributes) \
                or not all(attributes.values()):
            raise serializers.ValidationError(
                f'A variant needs 1 to {MAX_ATTRIBUTES} attributes, each with a value.')
        return attributes


class NewProductSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=256)
    unit = serializers.IntegerField()
    desc = serializers.CharField(required=False, allow_blank=True, default='')
    variants = VariantSerializer(many=True, required=False, default=list)


class NewVariantsSerializer(serializers.Serializer):
    variants = VariantSerializer(many=True, allow_empty=False)


def _validated(serializer_class, data):
    serializer = serializer_class(data=data)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


def _check_repeats(names, taken=()):
    """Two variants with the same name are the same variant."""
    existing = {name.lower() for name in taken}
    seen = set()
    for name in names:
        if name.lower() in existing:
            raise exceptions.ValidationError({'variants': [f'The product already has "{name}".']})
        if name.lower() in seen:
            raise exceptions.ValidationError({'variants': [f'"{name}" is on the list twice.']})
        seen.add(name.lower())


def create_product(business, data):
    """The product and its variants, no stock. Returns both, the variants
    first to last."""
    values = _validated(NewProductSerializer, data)
    if not Unit.objects.filter(pk=values['unit']).exists():
        raise exceptions.ValidationError({'unit': ['Pick a unit from the list.']})
    variants = [v['attributes'] for v in values['variants']]
    _check_repeats([generate_variant_name(attributes) for attributes in variants])

    with transaction.atomic():
        serializer = ProductAndVariantCreateSerializer(data={
            'name': values['name'].strip(), 'unit': values['unit'], 'desc': values['desc'],
            'variants': [{'attributes': attributes} for attributes in variants],
        }, context={'business_id': business.id})
        serializer.is_valid(raise_exception=True)
        product = serializer.save()
    return product, list(product.variants.order_by('id'))


def _business_product(business, product_id):
    product = (Product.objects.filter(pk=product_id, business_id=business.id, is_active=True)
               .select_related('unit').first())
    if not product:
        raise exceptions.NotFound('No such product.')
    return product


def product_for_adding(business, product_id):
    """A product the typed name matched, for the dialog to show: its details
    and the variants it has."""
    product = _business_product(business, product_id)
    return {
        'id': product.id,
        'name': product.name,
        'unit': {'id': product.unit_id, 'label': product.unit.name} if product.unit_id else None,
        'desc': product.desc or '',
        'variants': [{'id': v.id, 'attributes': v.attributes or {}, 'label': variant_label(v)}
                     for v in product.variants.filter(is_active=True).order_by('id')],
    }


def add_variants(business, product_id, data):
    """New variants on a product MarketPro has. Returns the product and the
    new variants, first to last."""
    product = _business_product(business, product_id)
    variants = [v['attributes'] for v in _validated(NewVariantsSerializer, data)['variants']]
    names = [generate_variant_name(attributes) for attributes in variants]
    _check_repeats(names, taken=list(product.variants.values_list('name', flat=True)))

    with transaction.atomic():
        made = [ProductVariant(base=product, sku=generate_sku(business_id=business.id),
                               name=name, attributes=attributes)
                for name, attributes in zip(names, variants)]
        audited_bulk_create(ProductVariant, made)
    return product, list(product.variants.filter(name__in=names).order_by('id'))


def answer(product, variants):
    return {
        'product': {'id': product.id, 'label': product.name},
        'variants': [{'id': v.id, 'label': variant_label(v)} for v in variants],
    }
