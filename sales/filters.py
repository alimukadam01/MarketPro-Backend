"""Filters for the sales and purchase invoice lists.

`payment_status` is a property rather than a column (SettledInvoiceMixin), so
DjangoFilterBackend cannot filter it: a ?payment_status= parameter used to be
accepted and silently ignored.

It used to be reproduced here in SQL from the invoice's own receipts. Since
credit and debit notes (credit-debit-notes plan), an invoice is also paid by
whatever its party's khaata settles onto it - opening balance first, then
oldest debts - and that matching cannot be expressed as a per-row subquery.
So the filter now asks the same property the table prints, after one khaata
pass per party (accounts.settlement.attach_settlement). The filter and the
column cannot disagree, because they are the same code.
"""
from django.utils import timezone
from django_filters import rest_framework as filters

from root.filter_ranges import date_range, number_range

from .models import PurchaseInvoice, PurchaseQuotation, ReturnedItem, SalesInvoice


def filter_by_payment_status(queryset, value):
    """
    The invoices in `queryset` whose payment_status is `value`, still as a
    queryset so ordering, search and the other filters compose with it.
    """
    # Function-level import: accounts imports sales at load time.
    from accounts.settlement import attach_settlement

    invoices = list(
        queryset.prefetch_related('payment_receipts__transaction_record'))
    attach_settlement(invoices)
    return queryset.filter(
        id__in=[invoice.id for invoice in invoices
                if invoice.payment_status == value])


class SalesInvoiceFilter(filters.FilterSet):

    # payment_status is derived (own receipts plus the khaata's pool), so these
    # three are the only values an invoice can ever report. The model also defines 'C' and 'RF',
    # but the property never returns them, and offering a choice that can match
    # nothing is worse than leaving it out.
    PAYMENT_STATUS_CHOICES = [
        ('P', 'PAID'),
        ('PP', 'PARTIALLY_PAID'),
        ('PEN', 'PENDING'),
    ]

    payment_status = filters.ChoiceFilter(
        choices=PAYMENT_STATUS_CHOICES, method='filter_payment_status')

    # Ranges rather than a single date per field. An exact match on a date is
    # almost never what a listing is filtered by, and on date_issued it could
    # not work at all: it is a DateTimeField, so an exact match would have to
    # be the precise instant, and every invoice is stored at midday.
    #
    # date__gte / date__lte on that field compares calendar dates in the
    # project's TIME_ZONE, which is the same day the table prints. A plain
    # gte on a DateTimeField would compare against midnight UTC and shift the
    # boundary by five hours.
    date_issued_from = filters.DateFilter(
        field_name='date_issued', lookup_expr='date__gte')
    date_issued_to = filters.DateFilter(
        field_name='date_issued', lookup_expr='date__lte')

    # date_due is a DateField, so it needs no __date step.
    date_due_from = filters.DateFilter(
        field_name='date_due', lookup_expr='gte')
    date_due_to = filters.DateFilter(
        field_name='date_due', lookup_expr='lte')

    # Overdue is derived on read, so the column only ever holds D, C or X and
    # an exact match on it would never return an overdue invoice - and would
    # wrongly include them under Completed.
    status = filters.ChoiceFilter(
        choices=SalesInvoice.SALES_INVOICE_STATUS_CHOICES,
        method='filter_status')

    # By id, not name: two customers can share a name, and the filter's
    # dropdown keeps them apart. customer__name stays for older clients.
    customer = filters.NumberFilter(field_name='customer')
    created_by = filters.NumberFilter(field_name='created_by')
    # An invoice is linked to a project through ProjectSalesInvoice.
    project = filters.NumberFilter(field_name='projects__project', distinct=True)
    total_min, total_max = number_range('total')

    class Meta:
        model = SalesInvoice
        # The columns that were filterable before, unchanged.
        fields = [
            'customer__name', 'sub_total', 'total',
            'is_deducted',
        ]

    def filter_status(self, queryset, name, value):
        """
        Draft and Cancelled are plain column matches. Completed and Overdue
        share the stored value 'C' and are told apart by what is still owed,
        so they are separated here.

        Narrowed in SQL first: only an invoice past its due date can be
        overdue, so the Python pass runs over a handful of rows rather than
        the whole table - unlike filter_by_payment_status, which has to load
        everything.
        """
        if value in ('D',) + tuple(SalesInvoice.CANCELLED_STATUSES):
            return queryset.filter(status=value)
        if value not in ('C', 'O'):
            return queryset

        from accounts.settlement import attach_settlement
        candidates = list(
            queryset.filter(status='C', date_due__lt=timezone.localdate())
            .prefetch_related('payment_receipts__transaction_record',
                              'invoice_items')
        )
        attach_settlement(candidates)
        overdue = [invoice.id for invoice in candidates if invoice.is_overdue]

        if value == 'O':
            return queryset.filter(id__in=overdue)
        return queryset.filter(status='C').exclude(id__in=overdue)

    def filter_payment_status(self, queryset, name, value):
        return filter_by_payment_status(queryset, value)


class PurchaseInvoiceFilter(filters.FilterSet):
    """
    The purchase-side twin of SalesInvoiceFilter.

    PurchaseInvoice.payment_status is derived exactly as the sales one is, so
    the same three values are the only ones an invoice can report, and the
    same function filters them.
    """

    PAYMENT_STATUS_CHOICES = SalesInvoiceFilter.PAYMENT_STATUS_CHOICES

    payment_status = filters.ChoiceFilter(
        choices=PAYMENT_STATUS_CHOICES, method='filter_payment_status')

    # See the sales filter above for why these are ranges, and why
    # date_issued needs the __date step that the other two do not.
    date_issued_from = filters.DateFilter(
        field_name='date_issued', lookup_expr='date__gte')
    date_issued_to = filters.DateFilter(
        field_name='date_issued', lookup_expr='date__lte')

    delivery_from = filters.DateFilter(
        field_name='delivery', lookup_expr='gte')
    delivery_to = filters.DateFilter(
        field_name='delivery', lookup_expr='lte')

    date_due_from = filters.DateFilter(
        field_name='date_due', lookup_expr='gte')
    date_due_to = filters.DateFilter(
        field_name='date_due', lookup_expr='lte')

    supplier = filters.NumberFilter(field_name='supplier')
    created_by = filters.NumberFilter(field_name='created_by')
    project = filters.NumberFilter(field_name='projects__project', distinct=True)
    total_min, total_max = number_range('total')

    class Meta:
        model = PurchaseInvoice
        # The columns that were filterable before, unchanged.
        fields = [
            'supplier__name', 'status', 'sub_total', 'total', 'goods_received',
        ]

    def filter_payment_status(self, queryset, name, value):
        return filter_by_payment_status(queryset, value)


class ReturnedItemFilter(filters.FilterSet):
    """The returned items list: by invoice number, product, customer, where
    the goods went and whether they came back damaged. The date is the page's
    period switch."""

    # The number on the invoice, matched in part - not the internal id the
    # old filter asked for, which nobody ever sees.
    invoice_number = filters.CharFilter(
        field_name='invoice_item__sales_invoice__invoice_number', lookup_expr='icontains')
    # The base product: one pick covers all its variants.
    product = filters.NumberFilter(field_name='invoice_item__product__base')
    customer = filters.NumberFilter(field_name='invoice_item__sales_invoice__customer')
    return_type = filters.ChoiceFilter(choices=ReturnedItem.RETURN_TYPES)
    is_damaged = filters.BooleanFilter()

    class Meta:
        model = ReturnedItem
        # The fields that were filterable before, unchanged.
        fields = [
            'invoice_item__sales_invoice__id', 'invoice_item__product__name',
            'invoice_item__product__base__name', 'quantity',
        ]


class PurchaseQuotationFilter(filters.FilterSet):
    """The purchase quotations list. It has no period switch, so its date is
    a range here."""

    status = filters.ChoiceFilter(choices=PurchaseQuotation.PQ_STATUSES)
    # A quotation asks several suppliers, one per item.
    supplier = filters.NumberFilter(field_name='items__supplier', distinct=True)
    project = filters.NumberFilter(field_name='projects__project', distinct=True)
    created_from, created_to = date_range('created_at', is_datetime=True)

    class Meta:
        model = PurchaseQuotation
        fields = []
