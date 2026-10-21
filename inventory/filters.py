"""Filter set for the stock list."""
from django.db.models import F, Q
from django_filters import rest_framework as filters

from root.filter_ranges import number_range

from .models import InventoryItem


class InventoryItemFilter(filters.FilterSet):
    STOCK_LEVELS = [
        ('in_stock', 'In stock'),
        ('low', 'Low'),
        ('out', 'Out of stock'),
    ]

    # The base product: one pick covers all its variants.
    product = filters.NumberFilter(field_name='product__base')
    location = filters.NumberFilter(field_name='location')
    stock_level = filters.ChoiceFilter(choices=STOCK_LEVELS, method='filter_stock_level')
    price_min, price_max = number_range('unit_price')

    class Meta:
        model = InventoryItem
        # The fields that were filterable before, unchanged for older clients.
        fields = ['inventory__id', 'location__name', 'product__base__name', 'product__name', 'track_code']

    def filter_stock_level(self, queryset, name, value):
        """
        out       nothing on hand
        low       something on hand, at or below its reorder level
        in_stock  on hand and above its reorder level (or with none set)
        """
        empty = Q(quantity_on_hand__lte=0)
        low = Q(quantity_on_hand__gt=0, reorder_level__isnull=False,
                quantity_on_hand__lte=F('reorder_level'))
        if value == 'out':
            return queryset.filter(empty)
        if value == 'low':
            return queryset.filter(low)
        if value == 'in_stock':
            return queryset.exclude(empty).exclude(low)
        return queryset
