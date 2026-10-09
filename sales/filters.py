"""Filters for the sales invoice list.

`payment_status` is a property rather than a column: it is derived from the
receipts recorded against the invoice, which is why nothing stores it
(SalesInvoice.payment_status). DjangoFilterBackend can only filter columns, so
a ?payment_status= parameter was accepted and then silently ignored - the list
came back unfiltered and looked like the filter had simply matched everything.

This reproduces the property in SQL so the filter and the column shown in the
table cannot disagree. The arithmetic is kept deliberately close to
SalesInvoice.payment_status; if that changes, this has to change with it.
"""
from django.db.models import F, FloatField, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django_filters import rest_framework as filters

from .models import (
    PurchaseInvoice, PurchaseReceipt, SalesInvoice, SalesReceipt,
)

# Only money that has cleared pays an invoice down. Mirrors
# SalesInvoice.amount_paid: a receipt whose transaction is pending or bounced
# has settled nothing, while one with no transaction at all predates the
# accounting module and always counts.
CLEARED_RECEIPT = (
    Q(transaction_record__isnull=True) | Q(transaction_record__status='C')
)


class SalesInvoiceFilter(filters.FilterSet):

    # payment_status is computed from the receipts, so these three are the only
    # values an invoice can ever report. The model also defines 'C' and 'RF',
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

    class Meta:
        model = SalesInvoice
        # The columns that were filterable before, unchanged.
        fields = [
            'customer__name', 'status', 'sub_total', 'total',
            'is_deducted', 'is_partially_deducted',
        ]

    def filter_payment_status(self, queryset, name, value):
        # Named amount_paid_value, not amount_paid: an annotation that shares a
        # name with the property would shadow it on every instance and the
        # serializer would then report the annotation instead.
        paid = Subquery(
            SalesReceipt.objects
            .filter(sales_invoice=OuterRef('pk'))
            .filter(CLEARED_RECEIPT)
            .values('sales_invoice')
            .annotate(cleared=Sum('amount'))
            .values('cleared')[:1],
            output_field=FloatField(),
        )
        queryset = queryset.annotate(
            amount_paid_value=Coalesce(paid, 0.0),
            # total is nullable, and NULL would make every comparison below
            # drop the row rather than treat it as unpaid.
            invoice_total=Coalesce(F('total'), 0.0),
        )

        if value == 'P':
            return queryset.filter(
                invoice_total__gt=0,
                amount_paid_value__gte=F('invoice_total'),
            )

        if value == 'PP':
            # Anything paid that is not fully paid. Written as the property's
            # second branch - paid > 0 having already failed the PAID test -
            # rather than as paid < total, which would also catch a zero-total
            # invoice with money against it.
            return queryset.filter(amount_paid_value__gt=0).exclude(
                invoice_total__gt=0,
                amount_paid_value__gte=F('invoice_total'),
            )

        if value == 'PEN':
            return queryset.filter(amount_paid_value__lte=0)

        return queryset


class PurchaseInvoiceFilter(filters.FilterSet):
    """
    The purchase-side twin of SalesInvoiceFilter.

    PurchaseInvoice.payment_status is derived exactly as the sales one is -
    cleared receipts against the total - so the same three values are the only
    ones an invoice can report, and the same SQL reproduces them.
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

    class Meta:
        model = PurchaseInvoice
        # The columns that were filterable before, unchanged.
        fields = [
            'supplier__name', 'status', 'sub_total', 'total', 'goods_received',
        ]

    def filter_payment_status(self, queryset, name, value):
        paid = Subquery(
            PurchaseReceipt.objects
            .filter(purchase_invoice=OuterRef('pk'))
            .filter(CLEARED_RECEIPT)
            .values('purchase_invoice')
            .annotate(cleared=Sum('amount'))
            .values('cleared')[:1],
            output_field=FloatField(),
        )
        queryset = queryset.annotate(
            amount_paid_value=Coalesce(paid, 0.0),
            invoice_total=Coalesce(F('total'), 0.0),
        )

        if value == 'P':
            return queryset.filter(
                invoice_total__gt=0,
                amount_paid_value__gte=F('invoice_total'),
            )

        if value == 'PP':
            return queryset.filter(amount_paid_value__gt=0).exclude(
                invoice_total__gt=0,
                amount_paid_value__gte=F('invoice_total'),
            )

        if value == 'PEN':
            return queryset.filter(amount_paid_value__lte=0)

        return queryset
