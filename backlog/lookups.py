"""
Type-ahead lists for the entry screens: an id and a label, nothing more.

The entry person sees names - the product, the customer, the account - and
never a figure: no stock, cost, price, balance or udhaar (§7). Each lookup
is one query, filtered to the business and to the text typed, capped.
"""
from django.db.models import Q

from accounts.models import MoneyAccount
from inventory.models import InventoryItem
from projects.models import Project
from root.models import City, Customer, Location, Product, ProductVariant, ProductVariantType, Supplier, Unit

from .outputs import variant_label

LIMIT = 25


def _words(query):
    return [word for word in (query or '').split() if word]


def _match(qs, query, fields):
    """Every word of the query must match one of the fields."""
    for word in _words(query):
        condition = Q()
        for field in fields:
            condition |= Q(**{f'{field}__icontains': word})
        qs = qs.filter(condition)
    return qs


def variants(business, query, with_stock=False):
    qs = (ProductVariant.objects
          .filter(base__business_id=business.id, is_active=True, base__is_active=True)
          .select_related('base', 'base__unit'))
    qs = _match(qs, query, ['base__name', 'name', 'sku', 'base__code'])
    rows = list(qs.order_by('base__name', 'name')[:LIMIT])
    # With its product's id and name, so a stock page's picker can tell a
    # name typed is an existing product's and offer to add variants to it.
    found = [{'id': v.id, 'label': variant_label(v), 'unit': v.base.unit.name if v.base.unit_id else '',
              'product': v.base_id, 'product_name': v.base.name} for v in rows]
    if with_stock:
        # A sales page labels what has run out, as the sales screens do -
        # yes or no only, never how many (section 7). Still pickable there:
        # an old bill is history.
        stocked = set(InventoryItem.objects.filter(
            business_id=business.id, product_id__in=[v.id for v in rows], quantity_on_hand__gt=0,
        ).values_list('product_id', flat=True))
        for row in found:
            row['in_stock'] = row['id'] in stocked
    return found


def products(business, query):
    qs = Product.objects.filter(business_id=business.id).select_related('unit')
    qs = _match(qs, query, ['name', 'code'])
    return [{'id': p.id, 'label': p.name, 'unit': p.unit.name if p.unit_id else ''}
            for p in qs.order_by('name')[:LIMIT]]


def customers(business, query):
    qs = _match(Customer.objects.filter(business_id=business.id), query, ['name', 'phone'])
    return [{'id': c.id, 'label': c.name} for c in qs.order_by('name')[:LIMIT]]


def suppliers(business, query):
    qs = _match(Supplier.objects.filter(business_id=business.id), query, ['name', 'business_name'])
    return [{'id': s.id, 'label': f'{s.name} ({s.business_name})' if s.business_name else s.name}
            for s in qs.order_by('name')[:LIMIT]]


def accounts(business, query):
    qs = _match(MoneyAccount.objects.filter(business_id=business.id, is_active=True), query, ['name'])
    return [{'id': a.id, 'label': a.name, 'type': a.type, 'is_default': a.is_default}
            for a in qs.order_by('-is_default', 'name')[:LIMIT]]


def units(business, query):
    qs = _match(Unit.objects.all(), query, ['name', 'abv'])
    return [{'id': u.id, 'label': u.name} for u in qs.order_by('name')[:LIMIT]]


def locations(business, query):
    qs = _match(Location.objects.filter(business_id=business.id), query, ['name'])
    return [{'id': l.id, 'label': l.name, 'is_default': l.is_default}
            for l in qs.order_by('-is_default', 'name')[:LIMIT]]


def attributes(business, query):
    qs = _match(ProductVariantType.objects.all(), query, ['name'])
    return [{'id': t.id, 'label': t.name} for t in qs.order_by('name')[:LIMIT]]


def cities(business, query):
    # Shared by every business, like units: a new customer needs one.
    qs = _match(City.objects.all(), query, ['name'])
    return [{'id': c.id, 'label': c.name} for c in qs.order_by('name')[:LIMIT]]


def projects(business, query):
    qs = _match(Project.objects.filter(business_id=business.id), query, ['name'])
    return [{'id': p.id, 'label': p.name} for p in qs.order_by('name')[:LIMIT]]


KINDS = {
    'variants': variants,
    'products': products,
    'customers': customers,
    'suppliers': suppliers,
    'accounts': accounts,
    'units': units,
    'locations': locations,
    'attributes': attributes,
    'projects': projects,
    'cities': cities,
}
