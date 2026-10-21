"""Filter set for the transactions list (the Accounting page)."""
from django.db.models import Q
from django_filters import rest_framework as filters

from root.filter_ranges import number_range

from .models import Transaction


class TransactionFilter(filters.FilterSet):
    # Who the money was with: the party on an on-account payment, or the
    # customer / supplier of the invoice a payment was recorded against.
    customer = filters.NumberFilter(method='filter_customer')
    supplier = filters.NumberFilter(method='filter_supplier')
    amount_min, amount_max = number_range('amount')

    class Meta:
        model = Transaction
        # The fields that were filterable before, unchanged. `date` stays for
        # older clients; the page uses its period switch instead.
        fields = ['type', 'status', 'payment_method', 'account', 'date']

    def filter_customer(self, queryset, name, value):
        return queryset.filter(
            Q(party_payment__customer=value) | Q(sales_receipt__sales_invoice__customer=value))

    def filter_supplier(self, queryset, name, value):
        return queryset.filter(
            Q(party_payment__supplier=value) | Q(purchase_receipt__purchase_invoice__supplier=value))
