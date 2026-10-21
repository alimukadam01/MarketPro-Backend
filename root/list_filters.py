"""Filter sets for root's list pages: expenses, products and customers.

In their own module rather than root/filters.py, which holds the old global
search engine due to be removed (see root/search).
"""
from django_filters import rest_framework as filters

from .filter_ranges import number_range
from .models import Customer, Expense, Product


class ExpenseFilter(filters.FilterSet):
    # Chosen from the category names, not typed as the stored code
    # ("kiraya"); the filter dialog offers the labels.
    category = filters.ChoiceFilter(choices=Expense.CATEGORY_CHOICES)
    amount_min, amount_max = number_range('amount')
    # The money account it was paid from, through its mirrored transaction.
    account = filters.NumberFilter(field_name='transaction_record__account')

    class Meta:
        model = Expense
        # The fields that were filterable before, unchanged for older clients.
        fields = ['name', 'desc', 'amount']


class ProductFilter(filters.FilterSet):
    unit = filters.NumberFilter(field_name='unit')
    is_active = filters.BooleanFilter()

    class Meta:
        model = Product
        fields = ['unit__name']


class CustomerFilter(filters.FilterSet):
    # By id: an exact-match text box on the city's name found nothing for a
    # typo or a different case.
    city = filters.NumberFilter(field_name='city')

    class Meta:
        model = Customer
        fields = ['city__name']
