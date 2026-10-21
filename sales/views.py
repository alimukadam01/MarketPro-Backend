from calendar import monthrange
from datetime import datetime

from django.db import transaction
from django.db.models import Avg, Count, F, Sum
from django.utils import timezone

from rest_framework import exceptions, status
from rest_framework.viewsets import GenericViewSet, ModelViewSet, ViewSet
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.filters import SearchFilter
from django_filters.rest_framework import DjangoFilterBackend

from root.serializers import CaptureCustomerSerializer
from root.periods import (
    TREND_PERIODS, PeriodFilterMixin, daily_trend, hourly_trend,
    in_period, is_filtered, period_from_params, period_payload,
)
from root.utils import get_active_business
from inventory.models import InventoryItem
from accounts.models import PartyPayment, Transaction
from accounts.settlement import attach_settlement
from accounts.utils import month_bounds, payables
from .filters import (
    PurchaseInvoiceFilter, PurchaseQuotationFilter, ReturnedItemFilter, SalesInvoiceFilter,
)
from .models import InvoiceImage, PurchaseInvoice, PurchaseInvoiceItem, PurchaseQuotation, PurchaseQuotationItem, PurchaseReceipt, ReturnedItem, SalesInvoice, SalesInvoiceItem, SalesReceipt
from .utils import build_whatsapp_payload, invoice_room, overpaid_by, restockOnCancel, sync_charge_expenses, updateInventoryOnSale
from .serializers import (
    PaymentReceiptSerializer,
    PurchaseInvoiceAndItemsCreateSerializer,
    PurchaseInvoiceAndItemsUpdateSerializer,
    PurchaseInvoiceCreateSerializer,
    PurchaseInvoiceItemCreateSerializer,
    PurchaseInvoiceItemSerializer,
    PurchaseInvoiceItemUpdateSerializer,
    PurchaseInvoiceUpdateSerializer,
    PurchaseInvoiceSerializer,
    PurchaseQuotationAndItemsCreateSerializer,
    PurchaseQuotationAndItemsUpdateSerializer,
    PurchaseQuotationCreateSerializer,
    PurchaseQuotationItemCreateUpdateSerializer,
    PurchaseQuotationItemSerializer,
    PurchaseQuotationSerializer,
    PurchaseReceiptCreateSerializer,
    RestockSerializer,
    ReturnedItemCreateUpdateSerializer,
    SalesInvoiceAndItemsCreateSerializer,
    SalesInvoiceAndItemsUpdateSerializer,
    SalesInvoiceCreateSerializer,
    SalesInvoiceItemCreateSerializer,
    SalesInvoiceItemSerializer,
    SalesInvoiceItemUpdateSerializer,
    ReturnedItemSerializer,
    SalesInvoiceSerializer,
    SalesInvoiceUpdateSerializer,
    SalesReceiptCreateSerializer,
    SimplePurchaseInvoiceItemSerializer,
    SimplePurchaseInvoiceSerializer,
    SimplePurchaseQuotationItemSerializer,
    SimpleSalesInvoiceItemSerializer,
    SimpleSalesInvoiceSerializer,
    GenerateInvoiceSerializer,
    InvoiceImageSerializer
)

# Create your views here.


class SettledInvoiceViewMixin:
    """
    What the khaata settles onto an invoice (credit-debit-notes plan, D9/D15).

    list() attaches the settlement for the whole filtered list in one khaata
    pass per party; without it every row's amount_paid would run its own.
    settlement/ is what the invoice's payments dialog reads: the khaata lines
    that settled the invoice, and the totals its "Paid in full" state and its
    payment cap are worked out from - so the dialog and the server agree.
    """

    def list(self, request, *args, **kwargs):
        invoices = list(self.filter_queryset(self.get_queryset()))
        attach_settlement(invoices)
        page = self.paginate_queryset(invoices)
        if page is not None:
            return self.get_paginated_response(
                self.get_serializer(page, many=True).data)
        return Response(self.get_serializer(invoices, many=True).data)

    @action(['GET'], detail=True, url_path='settlement', url_name='settlement')
    def settlement(self, request, pk=None):
        business = get_active_business(request)
        if not business:
            return Response(
                {'detail': 'No active business exists. Please contact admin.'},
                status=status.HTTP_400_BAD_REQUEST)
        try:
            invoice = self.get_object()
            attach_settlement([invoice])
            total = float(invoice.total or 0)
            paid = float(invoice.amount_paid or 0)
            return Response({
                'total': round(total),
                'paid_on_invoice': round(invoice.receipts_paid or 0),
                'settled_from_khaata': round(invoice.pool_applied or 0),
                'paid': round(paid),
                'still_owed': round(max(total - paid, 0)),
                # What one more payment naming this invoice may be. Less than
                # still_owed while a pending cheque holds part of it.
                'room': round(invoice_room(invoice)),
                'settled_by': invoice._settlement.settled_by(invoice.id),
                # Paid for more than it bills (the khaata's overpayment). The
                # dialog works out from this whether an edit it is showing
                # would create a refund, and how much.
                'overpaid': round(overpaid_by(invoice), 2),
                # What the saved invoice bills: its total, or 0 for a draft or
                # a cancelled invoice. The refund preview compares the edited
                # total against this, as refund_new_overpayment will.
                'billed': round(
                    0 if invoice.status in (invoice.CANCELLED_STATUSES
                                            + invoice.DRAFT_STATUSES)
                    else float(invoice.total or 0), 2),
                'refunds': self._refunds(invoice),
            }, status=status.HTTP_200_OK)
        except Exception as error:
            print(error)
            return Response({'detail': 'Internal Server Error.'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @staticmethod
    def _refund_rows(invoice):
        """The automatic refunds recorded for this invoice."""
        field = ('sales_invoice' if isinstance(invoice, SalesInvoice)
                 else 'purchase_invoice')
        return (PartyPayment.objects
                .filter(**{field: invoice})
                .select_related('transaction__account')
                .order_by('transaction__date', 'transaction__id'))

    def _refunds(self, invoice):
        return [
            {
                'id': row.transaction_id,
                'amount': row.transaction.amount,
                'date': row.transaction.date,
                'status': row.transaction.status,
                'account': ({'id': row.transaction.account_id,
                             'name': row.transaction.account.name,
                             'type': row.transaction.account.type}
                            if row.transaction.account_id else None),
            }
            for row in self._refund_rows(invoice)
        ]

    @action(['POST'], detail=True, url_path='delete-refund', url_name='delete-refund')
    def delete_refund(self, request, pk=None):
        """
        Delete one of this invoice's automatic refunds.

        The excess it paid back then stays with the party, as credit on their
        khaata. Only a refund recorded for THIS invoice can be deleted here,
        so an id from another invoice or business is a 404.
        """
        business = get_active_business(request)
        if not business:
            return Response(
                {'detail': 'No active business exists. Please contact admin.'},
                status=status.HTTP_400_BAD_REQUEST)

        transaction_id = request.data.get('transaction_id')
        if not transaction_id:
            return Response({'detail': 'Bad Request.'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            invoice = self.get_object()
            row = self._refund_rows(invoice).filter(
                transaction_id=transaction_id).first()
            if row is None:
                return Response({'detail': 'Not Found.'},
                                status=status.HTTP_404_NOT_FOUND)
            # The transaction owns the PartyPayment (CASCADE), so this removes
            # both.
            row.transaction.delete()
            return Response({'detail': 'Success.'}, status=status.HTTP_200_OK)
        except Exception as error:
            print(error)
            return Response({'detail': 'Internal Server Error.'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class PurchaseInvoiceViewSet(PeriodFilterMixin, SettledInvoiceViewMixin, ModelViewSet):

    filter_backends = [SearchFilter, DjangoFilterBackend]
    # A FilterSet rather than filterset_fields, so payment_status can be
    # filtered at all: it is a derived property, so there is no column for
    # DjangoFilterBackend to match and the parameter used to be ignored.
    # The column-backed fields are unchanged, declared in its Meta.
    filterset_class = PurchaseInvoiceFilter
    search_fields = [
        'id', 'invoice_number', 'supplier__name', 'status',
        'sub_total', 'total', 'goods_received', 'delivery', 'notes'
    ]

    def get_queryset(self):
        business = get_active_business(self.request)
        if not business:
            return []

        return PurchaseInvoice.objects.filter(
            business_id=business.id
        ).prefetch_related(
            'payment_receipts__transaction_record', 'invoice_items'
        ).order_by('-created_at')

    # The page's period switch narrows the table on the invoice date.
    period_lookup = 'date_issued__date'

    @action(['GET'], detail=False, url_path='summary', url_name='summary')
    def summary(self, request):
        """
        The Purchases cards, worked out from the rows the table shows: its
        search, its filters and the period. The dashboard calls it with a
        period alone.
        """
        business = get_active_business(request)
        if not business:
            return Response({
                'detail': 'No active business exists. Please contact admin.'
            }, status=status.HTTP_400_BAD_REQUEST)

        period, start, end = period_from_params(request.query_params)
        live = self.filter_queryset(self.get_queryset()).exclude(
            status__in=PurchaseInvoice.CANCELLED_STATUSES)
        figures = live.aggregate(invoiced=Sum('total'), count=Count('id'))

        return Response({
            'purchases_invoiced': round(figures['invoiced'] or 0),
            'invoices_raised': figures['count'],
            'payments_made': round(self._money_paid(request, business, start, end)),
            'filtered': is_filtered(request),
            'period': period_payload(period, start, end),
        }, status=status.HTTP_200_OK)

    def _money_paid(self, request, business, start, end):
        # Unfiltered, this is all money paid for purchases in the period, on
        # account included - the figure the old card showed. A search or a
        # filter selects invoices, and money paid on account names none, so
        # then only payments against the matching invoices can count.
        if not is_filtered(request):
            return Transaction.objects.money_for_types(
                business.id, Transaction.PURCHASE_MONEY_TYPES, start, end)
        matched = self.filter_queryset_without_period(self.get_queryset())
        money = Transaction.objects.filter(
            business_id=business.id,
            purchase_receipt__purchase_invoice__in=matched,
        ).cleared()
        return in_period(money, 'date', start, end).aggregate(
            total=Sum('amount'))['total'] or 0

    def get_serializer_class(self):
        method = self.request.method

        if self.action == 'create_invoice_and_items' and method == 'POST':
            return PurchaseInvoiceAndItemsCreateSerializer
        if self.action == 'update_invoice_and_items' and method == 'POST':
            return PurchaseInvoiceAndItemsUpdateSerializer
        if method == 'POST':
            return PurchaseInvoiceCreateSerializer
        elif method in ('PUT', 'PATCH'):
            return PurchaseInvoiceUpdateSerializer
        elif self.action == 'list':
            return SimplePurchaseInvoiceSerializer

        return PurchaseInvoiceSerializer

    def get_serializer_context(self):

        business = get_active_business(self.request)
        if not business:
            return {}
        if self.action == 'detail':
            return {
                'business_id': business.id,
                'purchase_invoice_id': self.kwargs['pk']
            }

        return {
            'business_id': business.id,
            'user_id': self.request.user.id
        }

    @action(['POST'], detail=True)
    def restock(self, request, pk=None):

        if request.method == 'POST':

            business = get_active_business(request)
            if not business:
                return Response({
                    'detail': 'No active business exists. Please contact admin.'
                }, status=status.HTTP_400_BAD_REQUEST)

            serializer = RestockSerializer(data=request.data, context={
                'purchase_invoice_id': self.kwargs['pk'],
                'business_id': business.id,
                'inventory_id': business.inventory_glance.id
            })

            try:
                serializer.is_valid(raise_exception=True)
                is_restocked = serializer.save()
            except Exception as error:
                print(error)
                return Response({
                    'detail': 'Internal Server Error'
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

            if not is_restocked:
                return Response({
                    'detail': 'Bad Request.'
                }, status=status.HTTP_400_BAD_REQUEST)

            return Response({
                'detail': 'OK'
            }, status=status.HTTP_200_OK)

    @action(['POST'], detail=False, url_path='create-with-items', url_name='create-with-items')
    def create_invoice_and_items(self, request):
        try:
            business = get_active_business(request)
            if not business:
                return Response({
                    'detail': 'No active business exists. Please contact admin.'
                }, status=status.HTTP_400_BAD_REQUEST)

            serializer = PurchaseInvoiceAndItemsCreateSerializer(
                data=request.data,
                context={
                    'business_id': business.id,
                    'user_id': self.request.user.id,
                })
            serializer.is_valid(raise_exception=True)
            purchase_invoice = serializer.save()

            if not purchase_invoice:
                return Response({
                    'detail': 'Bad Request.'
                }, status=status.HTTP_400_BAD_REQUEST)

            res_serializer = PurchaseInvoiceSerializer(purchase_invoice)
            return Response(res_serializer.data, status=status.HTTP_201_CREATED)

        except Exception as error:
            print(error)
            return Response({
                'detail': 'Internal Server Error.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(['POST'], detail=False, url_path='bulk-delete', url_name='bulk-delete')
    def bulk_delete(self, request):
        invoice_ids = request.data.get('invoice_ids', [])
        if not invoice_ids:
            return Response({
                'detail': 'Bad Request.'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            PurchaseInvoice.objects.filter(id__in=invoice_ids).delete()
            return Response({
                'detail': 'Success.'
            }, status=status.HTTP_200_OK)

        except Exception as error:
            print(error)
            return Response({
                'detail': 'Internal Server Error.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(['POST'], detail=True, url_path='update-with-items', url_name='update-with-items')
    def update_invoice_and_items(self, request, pk=None):
        try:
            business = get_active_business(self.request)
            if not business:
                return Response({
                    'detail': 'Not Found.'
                }, status=status.HTTP_404_NOT_FOUND)

            instance = self.get_object()
            serializer = PurchaseInvoiceAndItemsUpdateSerializer(
                instance=instance,
                data=request.data,
                context={
                    'business_id': business.id,
                    'user_id': self.request.user.id
                }
            )
            serializer.is_valid(raise_exception=True)
            updated_purchase_invoice = serializer.save()

            if not updated_purchase_invoice:
                return Response({
                    'detail': 'Bad Request.'
                }, status=status.HTTP_400_BAD_REQUEST)

            res_serializer = PurchaseInvoiceSerializer(updated_purchase_invoice)
            return Response(res_serializer.data, status=status.HTTP_200_OK)

        except exceptions.APIException:
            # A validation error goes out as the 400 it is, with its reason,
            # as the sales update does.
            raise

        except Exception as error:
            print(error)
            return Response({
                'detail': 'Internal Server Error.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class PurchaseInvoiceItemViewSet(ModelViewSet):

    filter_backends = [SearchFilter, DjangoFilterBackend]
    filterset_fields = [
        'purchase_invoice__id', 'purchase_invoice__invoice_number', 'is_restocked', 'is_partially_restocked',
        'product__name', 'product__base__name', 'track_code', 'unit_cost', 'quantity_received',
    ]
    search_fields = [
        'id', 'purchase_invoice__id', 'purchase_invoice__invoice_number', 'product__name',
        'product__base__name', 'track_code', 'notes', 'unit_cost', 'quantity_received'
    ]

    def get_serializer_class(self):

        if self.action == 'list':
            return SimplePurchaseInvoiceItemSerializer

        if self.request.method == 'POST':
            return PurchaseInvoiceItemCreateSerializer

        if self.request.method in ('PUT', 'PATCH'):
            return PurchaseInvoiceItemUpdateSerializer

        return PurchaseInvoiceItemSerializer

    def get_queryset(self):
        return PurchaseInvoiceItem.objects.filter(
            purchase_invoice_id=self.kwargs['purchase_invoice_pk']
        )

    def get_serializer_context(self):

        business = get_active_business(self.request)
        if not business:
            return {}
        return {
            'purchase_invoice_id': self.kwargs['purchase_invoice_pk'],
            'business_id': business.id
        }


class PurchaseReceiptsViewSet(ModelViewSet):

    def get_queryset(self):
        return PurchaseReceipt.objects.filter(purchase_invoice_id = self.kwargs['purchase_invoice_pk'])
    
    def get_serializer_class(self):
        
        if self.request.method == 'POST':
            return PurchaseReceiptCreateSerializer

        return PaymentReceiptSerializer
    
    def get_serializer_context(self):
        return {
            # request is what turns a payment's image into an absolute URL.
            # A context without it replaces DRF's default, which is how every
            # transaction image once came back as a bare /media/... path.
            'request': self.request,
            "purchase_invoice_id": self.kwargs['purchase_invoice_pk']
        }


class PurchasesKPIViewSet(GenericViewSet):

    # MONTHLY METRICS
    @action(['GET'], detail=False, url_name='monthly-total-purchases', url_path='monthly-total-purchases')
    def monthly_total_purchases(self, request):

        if request.method == 'GET':

            business = get_active_business(request)
            if not business:
                return Response({
                    'detail': 'No active business exists. Please contact admin.'
                }, status=status.HTTP_400_BAD_REQUEST)

            month_start, month_end = month_bounds()
            total_purchases = Transaction.objects.money_for_types(
                business.id, Transaction.PURCHASE_MONEY_TYPES,
                month_start, month_end)
            return Response({
                "total_purchases": total_purchases
            }, status=status.HTTP_200_OK)

        return Response({
            "detail": "Method not allowed"
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    @action(['GET'], detail=False, url_name='monthly-total-purchase-invoices', url_path='monthly-total-purchase-invoices')
    def monthly_total_invoices(self, request):
        if request.method == 'GET':

            business = get_active_business(request)
            if not business:
                return Response({
                    'detail': 'No active business exists. Please contact admin.'
                }, status=status.HTTP_400_BAD_REQUEST)

            # The calendar month, dated on the invoice.
            #
            # This read `total_invoices(business.id, today.day)`, which was
            # wrong twice over. in_period() counts days back from now against
            # created_at - when the row was typed, not when the purchase
            # happened - so every invoice entered in a migration or a catch-up
            # session counted as this month's whatever its date. And today.day
            # is the day of the month, used as a number of days: a rolling
            # window that is one day long on the 1st and reaches into the
            # previous month on the 31st, so it was never the current month.
            #
            # purchase_invoice_count already dates on date_issued and leaves
            # out cancelled invoices, which is what purchase_value does for
            # the money figure beside this one.
            start, end = month_bounds()
            total_invoices = PurchaseInvoice.objects.purchase_invoice_count(
                business.id, start, end)
            return Response({
                "total_invoices": total_invoices
            }, status=status.HTTP_200_OK)

        return Response({
            "detail": "Method not allowed"
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    # All TIME METRICS
    @action(['GET'], detail=False, url_name='total-purchases', url_path='total-purchases')
    def total_purchases(self, request):
        if request.method == 'GET':
            business = get_active_business(request)
            if not business:
                return Response({
                    'detail': 'No active business exists. Please contact admin.'
                }, status=status.HTTP_400_BAD_REQUEST)

            total_purchases = Transaction.objects.money_for_types(
                business.id, Transaction.PURCHASE_MONEY_TYPES)
            return Response({
                "total_purchases": total_purchases
            }, status=status.HTTP_200_OK)

        return Response({
            "detail": "Method not allowed"
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    @action(['GET'], detail=False, url_name='total-pending-invoices', url_path='total-pending-invoices')
    def total_pending_invoices(self, request):
        if request.method == 'GET':

            business = get_active_business(request)
            if not business:
                return Response({
                    'detail': 'No active business exists. Please contact admin.'
                }, status=status.HTTP_400_BAD_REQUEST)

            total_invoices = PurchaseInvoice.objects.total_pending_invoices(
                business.id)
            return Response({
                "total_invoices": total_invoices
            }, status=status.HTTP_200_OK)

        return Response({
            "detail": "Method not allowed"
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    @action(['GET'], detail=False, url_name='total-pending-payment', url_path='total-pending-payment')
    def total_pending_paymnet(self, request):
        if request.method == 'GET':

            business = get_active_business(request)
            if not business:
                return Response({
                    'detail': 'No active business exists. Please contact admin.'
                }, status=status.HTTP_400_BAD_REQUEST)

            total_payment = payables(business.id)['total']
            return Response({
                "total_payment": total_payment
            }, status=status.HTTP_200_OK)

        return Response({
            "detail": "Method not allowed"
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)


class SalesInvoiceViewSet(PeriodFilterMixin, SettledInvoiceViewMixin, ModelViewSet):

    filter_backends = [SearchFilter, DjangoFilterBackend]
    # A FilterSet rather than filterset_fields, so payment_status can be
    # filtered at all: it is a derived property, so there is no column for
    # DjangoFilterBackend to match and the parameter used to be ignored.
    # The column-backed fields are unchanged, declared in its Meta.
    filterset_class = SalesInvoiceFilter
    # 'status' is deliberately absent. Search is an icontains across every
    # field at once, and the column holds one-letter codes - so searching for
    # anything with a "c" in it matched every completed invoice, and a search
    # for a customer called Dawood matched every draft.
    search_fields = [
        'id', 'invoice_number', 'customer__name', 'sub_total', 'total', 'discount', 'tax', 'notes', 'created_by__email'
    ]

    def get_queryset(self):
        business = get_active_business(self.request)
        if not business:
            return []

        return SalesInvoice.objects.filter(
            business_id=business.id
        ).prefetch_related(
            'payment_receipts__transaction_record', 'invoice_items'
        ).order_by("-created_at")

    # The page's period switch narrows the table on the invoice date.
    period_lookup = 'date_issued__date'

    @action(['GET'], detail=False, url_path='summary', url_name='summary')
    def summary(self, request):
        """
        The Sales cards, worked out from the rows the table shows: its search,
        its filters and the period. The dashboard calls it with a period
        alone.

        Sales invoiced is what was billed; payments received is the money that
        came in. They differ by everything sold on credit and every old debt
        paid off, which is why they are two cards.
        """
        business = get_active_business(request)
        if not business:
            return Response({
                'detail': 'No active business exists. Please contact admin.'
            }, status=status.HTTP_400_BAD_REQUEST)

        period, start, end = period_from_params(request.query_params)
        live = self.filter_queryset(self.get_queryset()).exclude(
            status__in=SalesInvoice.CANCELLED_STATUSES)
        # Avg drops the NULL totals of invoices with no lines, so an empty
        # draft is not counted as an order worth nothing.
        figures = live.aggregate(
            invoiced=Sum('total'), average=Avg('total'), count=Count('id'))
        items = SalesInvoiceItem.objects.filter(sales_invoice__in=live).aggregate(
            sold=Sum(F('quantity') - F('returned_quantity')))['sold']

        return Response({
            'sales_invoiced': round(figures['invoiced'] or 0),
            'invoices_raised': figures['count'],
            'items_sold': items or 0,
            'average_order_value': round(figures['average'] or 0),
            'payments_received': round(self._money_received(request, business, start, end)),
            'filtered': is_filtered(request),
            'period': period_payload(period, start, end),
        }, status=status.HTTP_200_OK)

    def _money_received(self, request, business, start, end):
        # Unfiltered, this is all money received for sales in the period,
        # khaata receipts included - the figure the old card showed. A search
        # or a filter selects invoices, and a khaata receipt names none, so
        # then only payments against the matching invoices can count.
        if not is_filtered(request):
            return Transaction.objects.money_for_types(
                business.id, Transaction.SALES_MONEY_TYPES, start, end)
        matched = self.filter_queryset_without_period(self.get_queryset())
        money = Transaction.objects.filter(
            business_id=business.id,
            sales_receipt__sales_invoice__in=matched,
        ).cleared()
        return in_period(money, 'date', start, end).aggregate(
            total=Sum('amount'))['total'] or 0

    def get_serializer_class(self):
        method = self.request.method

        if self.action == 'create_invoice_and_items' and method == 'POST':
            return SalesInvoiceAndItemsCreateSerializer
        if self.action == 'update_invoice_and_items' and method == 'POST':
            return SalesInvoiceAndItemsUpdateSerializer
        if method == 'POST':
            return SalesInvoiceCreateSerializer
        elif method in ('PUT', 'PATCH'):
            return SalesInvoiceUpdateSerializer
        elif self.action == 'list':
            return SimpleSalesInvoiceSerializer

        return SalesInvoiceSerializer

    def get_serializer_context(self):
        business = get_active_business(self.request)
        if not business:
            return {}
        return {
            'business_id': business.id,
            'user_id': self.request.user.id
        }

    @action(['POST'], detail=False, url_path='create-with-items', url_name='create-with-items')
    def create_invoice_and_items(self, request):
        if request.method == 'POST':
            try:
                business = get_active_business(request)
                if not business:
                    return Response({
                        'detail': 'No active business exists. Please contact admin.'
                    }, status=status.HTTP_400_BAD_REQUEST)

                serializer = SalesInvoiceAndItemsCreateSerializer(data=request.data, context={
                    'business_id': business.id,
                    'user_id': self.request.user.id,
                })
                serializer.is_valid(raise_exception=True)
                sales_invoice = serializer.save()

                if not sales_invoice:
                    return Response({
                        'detail': 'Bad Request.'
                    }, status=status.HTTP_400_BAD_REQUEST)

                res_serializer = SalesInvoiceSerializer(sales_invoice)
                return Response(res_serializer.data, status=status.HTTP_201_CREATED)

            except exceptions.APIException:
                # Let a validation error through. is_valid(raise_exception=True)
                # raises one, and the blanket handler below was turning it into
                # a 500 with no message - so every reason a payload was
                # rejected, down to which extra charge was wrong, was lost.
                raise

            except Exception as error:
                print(error)
                return Response({
                    'detail': 'Internal Server Error.'
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(['POST'], detail=False, url_path='bulk-delete', url_name='bulk-delete')
    def bulk_delete(self, request):
        invoice_ids = request.data.get('invoice_ids', [])
        if not invoice_ids:
            return Response({
                'detail': 'Bad Request.'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            SalesInvoice.objects.filter(id__in=invoice_ids).delete()
            return Response({
                'detail': 'Success.'
            }, status=status.HTTP_200_OK)

        except Exception as error:
            print(error)
            return Response({
                'detail': 'Internal Server Error.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(['POST'], detail=True, url_path='update-with-items', url_name='update-with-items')
    def update_invoice_and_items(self, request, pk=None):
        if request.method == 'POST':
            try:
                business = get_active_business(request)
                if not business:
                    return Response({
                        'detail': 'No active business exists. Please contact admin.'
                    }, status=status.HTTP_400_BAD_REQUEST)

                instance = self.get_object()
                if not instance:
                    return Response({
                        'detail': 'Not Found.'
                    }, status=status.HTTP_404_NOT_FOUND)

                serializer = SalesInvoiceAndItemsUpdateSerializer(instance, data=request.data, context={
                    'business_id': business.id,
                    'user_id': self.request.user.id,
                })
                serializer.is_valid(raise_exception=True)
                sales_invoice = serializer.save()

                if not sales_invoice:
                    return Response({
                        'detail': 'Bad Request.'
                    }, status=status.HTTP_400_BAD_REQUEST)

                res_serializer = SalesInvoiceSerializer(sales_invoice)
                return Response(res_serializer.data, status=status.HTTP_200_OK)

            except exceptions.APIException:
                # Let a validation error through. is_valid(raise_exception=True)
                # raises one, and the blanket handler below was turning it into
                # a 500 with no message - so every reason a payload was
                # rejected, down to which extra charge was wrong, was lost.
                raise

            except Exception as error:
                print(error)
                return Response({
                    'detail': 'Internal Server Error.'
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(['GET'], detail=True, url_path='print-invoice', url_name='print-invoice')
    def print_invoice(self, request, pk=None):
        if request.method == "GET":
            sales_invoice = self.get_object()
            serializer = GenerateInvoiceSerializer(sales_invoice)

            return Response(serializer.data, status=status.HTTP_200_OK)

    ### the message the shopkeeper sends the customer along with the invoice
    @action(['GET'], detail=True, url_path='whatsapp-message', url_name='whatsapp-message')
    def whatsapp_message(self, request, pk=None):

        payload = build_whatsapp_payload(self.get_object())

        if not payload:
            return Response({
                'detail': 'This customer has no phone number saved.'
            }, status=status.HTTP_400_BAD_REQUEST)

        return Response(payload, status=status.HTTP_200_OK)

    ### a counter sale gets a name when it is printed
    @action(['POST'], detail=True, url_path='cancel', url_name='cancel')
    def cancel(self, request, pk=None):
        """
        Cancel an invoice and return its goods to stock.

        The one status transition a person makes: every other value is derived
        from the invoice's contents, so cancelling is an action rather than a
        field. A cancelled invoice is not a debt, so it also leaves the party's
        khaata.
        """
        business = get_active_business(request)
        if not business:
            return Response(
                {'detail': 'No active business exists. Please contact admin.'},
                status=status.HTTP_400_BAD_REQUEST)

        try:
            invoice = self.get_object()
            if invoice.status in SalesInvoice.CANCELLED_STATUSES:
                return Response({'detail': 'This invoice is already cancelled.'},
                                status=status.HTTP_400_BAD_REQUEST)

            with transaction.atomic():
                # Stock first: restockOnCancel reads the deduction ledger, and
                # it is cleared here so un-cancelling deducts exactly once more.
                restockOnCancel(invoice)
                invoice.status = 'X'
                SalesInvoice.objects.filter(pk=invoice.pk).update(status='X')
                # A cancelled sale paid out no charges: their expenses go too.
                sync_charge_expenses(invoice)

            invoice.refresh_from_db()
            return Response(SalesInvoiceSerializer(invoice).data,
                            status=status.HTTP_200_OK)
        except Exception as error:
            print(error)
            return Response({'detail': 'Internal Server Error.'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(['POST'], detail=True, url_path='uncancel', url_name='uncancel')
    def uncancel(self, request, pk=None):
        """
        Reinstate a cancelled invoice: the status goes back to being derived
        from its lines, and the goods leave stock again.
        """
        business = get_active_business(request)
        if not business:
            return Response(
                {'detail': 'No active business exists. Please contact admin.'},
                status=status.HTTP_400_BAD_REQUEST)

        try:
            invoice = self.get_object()
            if invoice.status not in SalesInvoice.CANCELLED_STATUSES:
                return Response({'detail': 'This invoice is not cancelled.'},
                                status=status.HTTP_400_BAD_REQUEST)

            with transaction.atomic():
                # Out of the manual state first, so recalculate_status stops
                # treating it as sticky and can derive from the lines again.
                invoice.status = 'D'
                SalesInvoice.objects.filter(pk=invoice.pk).update(status='D')
                invoice.recalculate_status()
                updateInventoryOnSale(invoice)
                # And the charge expenses come back, if it has been paid.
                sync_charge_expenses(invoice)

            invoice.refresh_from_db()
            return Response(SalesInvoiceSerializer(invoice).data,
                            status=status.HTTP_200_OK)
        except Exception as error:
            print(error)
            return Response({'detail': 'Internal Server Error.'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(['POST'], detail=True, url_path='capture-customer', url_name='capture-customer')
    def capture_customer(self, request, pk=None):
        """
        Turn a walk-in invoice into a named one.

        The invoice was raised against the business's placeholder customer
        because the buyer was not known at the till. They are captured here,
        when the invoice is printed, and both writes land together — the client
        can never be left holding a customer that no invoice points at.
        """
        business = get_active_business(request)
        if not business:
            return Response({
                'detail': 'No active business exists. Please contact admin.'
            }, status=status.HTTP_400_BAD_REQUEST)

        invoice = self.get_object()

        serializer = CaptureCustomerSerializer(
            data=request.data, context={'business_id': business.id})
        serializer.is_valid(raise_exception=True)

        try:
            with transaction.atomic():
                customer = serializer.save()
                # .update() rather than invoice.save(): a one-column write, and
                # nothing else should be touched by a change of buyer. It used
                # to be here to dodge a post_save receiver that ran a stock
                # deduction pass on any invoice save; that receiver is gone.
                SalesInvoice.objects.filter(pk=invoice.pk).update(customer=customer)

            invoice.refresh_from_db()

            return Response({
                'invoice': GenerateInvoiceSerializer(invoice).data,
                # Built after the commit, and never raises — a message that
                # will not format is no reason to undo a correct reassignment.
                'whatsapp': build_whatsapp_payload(invoice),
            }, status=status.HTTP_200_OK)

        except Exception as error:
            print(error)
            return Response({
                'detail': 'Internal Server Error.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class SalesInvoiceItemViewSet(ModelViewSet):

    filter_backends = [SearchFilter, DjangoFilterBackend]
    filterset_fields = [
        'sales_invoice__id', 'sales_invoice__invoice_number', 'is_deducted',
        'product__name', 'product__base__name', 'track_code', 'unit_price', 'quantity_received'
    ]
    search_fields = [
        'id', 'sales_invoice__id', 'sales_invoice__invoice_number', 'product__name', 'product__base__name', 'track_code', 'quantity_received', 'unit_price', 'discount'
    ]

    def get_queryset(self):
        business = get_active_business(self.request)
        if not business:
            return []

        return SalesInvoiceItem.objects.filter(business_id=business.id, sales_invoice_id=self.kwargs['sales_invoice_pk'])

    def get_serializer_class(self):
        method = self.request.method

        if self.action == 'return_item':
            return ReturnedItemCreateUpdateSerializer

        if self.action == 'list':
            return SimpleSalesInvoiceItemSerializer

        if method == 'POST':
            return SalesInvoiceItemCreateSerializer

        if method in ('PUT', 'PATCH'):
            return SalesInvoiceItemUpdateSerializer

        return SalesInvoiceItemSerializer

    def get_serializer_context(self):

        business = get_active_business(self.request)
        if not business:
            return {}
        return {
            'business_id': business.id,
            'sales_invoice_id': self.kwargs['sales_invoice_pk'],
            'inventory_id': business.inventory_glance.id
        }

    @action(['POST'], detail=True, url_path='return', url_name='return')
    def return_item(self, request, pk=None, sales_invoice_pk=None):
        if request.method == 'POST':
            business = get_active_business(request)
            if not business:
                return Response({
                    'detail': 'No active business exists. Please contact admin.'
                }, status=status.HTTP_400_BAD_REQUEST)

            serializer = ReturnedItemCreateUpdateSerializer(data=request.data, context={
                'business_id': business.id,
                'invoice_item_id': self.kwargs['pk']
            })

            try:
                serializer.is_valid(raise_exception=True)
                returned_item = serializer.save()

                if not returned_item:
                    return Response({
                        'detail': 'Bad Request.'
                    }, status=status.HTTP_400_BAD_REQUEST)

                return Response({
                    'detail': 'OK'
                }, status=status.HTTP_200_OK)

            except Exception as error:
                print(error)
                return Response({
                    'detail': 'Internal Server Error'
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class SalesReceiptsViewSet(ModelViewSet):

    def get_queryset(self):
        return SalesReceipt.objects.filter(sales_invoice_id = self.kwargs['sales_invoice_pk'])
    
    def get_serializer_class(self):
        if self.request.method == 'POST':
            return SalesReceiptCreateSerializer
        
        return PaymentReceiptSerializer
    
    def get_serializer_context(self):
        return {
            # See PurchaseReceiptsViewSet: without request, a payment's image
            # is a host-less path the frontend cannot load.
            'request': self.request,
            "sales_invoice_id": self.kwargs['sales_invoice_pk']
        }


class SalesKPIViewSet(GenericViewSet):

    queryset = []
    serializer_class = None

    # DAILY METRICS
    @action(['GET'], detail=False, url_name='daily-total-sales', url_path='daily-total-sales')
    def daily_total_sales(self, request):
        if request.method == 'GET':

            business = get_active_business(request)
            if not business:
                return Response({
                    'detail': 'No active business exists. Please contact admin.'
                }, status=status.HTTP_400_BAD_REQUEST)

            today = timezone.localdate()
            total_sales = Transaction.objects.money_for_types(
                business.id, Transaction.SALES_MONEY_TYPES, today, today)
            return Response({
                "total_daily_sales": total_sales
            }, status=status.HTTP_200_OK)

        return Response({
            "detail": "Method not allowed"
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    @action(['GET'], detail=False, url_name='daily-total-sales-invoices', url_path='daily-total-sales-invoices')
    def daily_total_invoices(self, request):
        if request.method == 'GET':

            business = get_active_business(request)
            if not business:
                return Response({
                    'detail': 'No active business exists. Please contact admin.'
                }, status=status.HTTP_400_BAD_REQUEST)

            # Dated on the invoice, not on created_at. total_invoices() went
            # through BaseQuerySet.in_period, which filters created_at over a
            # rolling 24 hours, so an invoice backdated to yesterday was
            # counted today and a late-entered one was counted on the wrong
            # day. invoice_count() filters date_issued and drops cancelled
            # invoices - the same fix daily-total-items and
            # monthly-total-purchase-invoices already carry.
            today = timezone.localdate()
            total_invoices = SalesInvoice.objects.invoice_count(
                business.id, today, today)
            return Response({
                "total_invoices": total_invoices
            }, status=status.HTTP_200_OK)

        return Response({
            "detail": "Method not allowed"
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    @action(['GET'], detail=False, url_name='recent-sales', url_path='recent-sales')
    def recent_sales(self, request):
        business = get_active_business(request)
        if not business:
            return Response({
                'detail': 'No active business exists. Please contact admin.'
            }, status=status.HTTP_400_BAD_REQUEST)

        recent_sales = SalesInvoice.objects.recent_sales(business.id)
        return Response({
            "recent_sales": recent_sales
        }, status=status.HTTP_200_OK)

    @action(methods=['GET'], detail=False, url_name='daily-total-items', url_path='daily-total-items')
    def daily_total_items(self, request):
        if request.method == "GET":
            business = get_active_business(request)
            if not business:
                return Response({
                    'detail': 'No active business exists. Please contact admin.'
                }, status=status.HTTP_400_BAD_REQUEST)

            # Today's calendar day, dated on the invoice.
            #
            # This read total_items_sold(business.id, 1), which was wrong four
            # ways. in_period(1) is a rolling 24 hours against the ITEM's own
            # created_at, so "sold today" meant "typed in since this time
            # yesterday" - every line of a migration counted, and a sale made
            # this morning did not. It also summed gross quantity, so a
            # returned unit still counted as sold, and it counted lines on
            # cancelled invoices.
            #
            # net_items_sold answers the question the card asks: it dates on
            # sales_invoice__date_issued, leaves out cancelled invoices, and
            # subtracts returned_quantity in SQL.
            today = timezone.localdate()
            total_items = SalesInvoiceItem.objects.net_items_sold(
                business.id, today, today)
            return Response({
                "total_items": total_items
            }, status=status.HTTP_200_OK)

        return Response({
            "detail": "Method not allowed"
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    # MONTHLY METRICS
    @action(['GET'], detail=False, url_name='monthly-total-sales', url_path='monthly-total-sales')
    def monthly_total_sales(self, request):

        if request.method == 'GET':

            business = get_active_business(request)
            if not business:
                return Response({
                    'detail': 'No active business exists. Please contact admin.'
                }, status=status.HTTP_400_BAD_REQUEST)

            month_start, month_end = month_bounds()
            total_sales = Transaction.objects.money_for_types(
                business.id, Transaction.SALES_MONEY_TYPES,
                month_start, month_end)
            return Response({
                "total_monthly_sales": total_sales
            }, status=status.HTTP_200_OK)

        return Response({
            "detail": "Method not allowed"
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    @action(['GET'], detail=False, url_name='monthly-sales-trend', url_path='monthly-sales-trend')
    def monthly_sales_trend(self, request):

        if request.method == 'GET':

            business = get_active_business(request)
            if not business:
                return Response({
                    'detail': 'No active business exists. Please contact admin.'
                }, status=status.HTTP_400_BAD_REQUEST)

            total_sales = Transaction.objects.monthly_type_trend(
                business.id, Transaction.SALES_MONEY_TYPES)
            return Response({
                "monthly_sales_trend": total_sales
            }, status=status.HTTP_200_OK)

        return Response({
            "detail": "Method not allowed"
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    @action(['GET'], detail=False, url_name='sales-trend', url_path='sales-trend')
    def sales_trend(self, request):
        """
        Sales money over the dashboard chart's period: by hour today, by day
        for a week or a month, by week for a quarter, by month for a year.

        Transaction.date carries no time, so today's hours go by when each
        payment was entered.
        """
        business = get_active_business(request)
        if not business:
            return Response({
                'detail': 'No active business exists. Please contact admin.'
            }, status=status.HTTP_400_BAD_REQUEST)

        period, start, end = period_from_params(
            request.query_params, default='month', allowed=TREND_PERIODS)
        money = in_period(
            Transaction.objects.filter(
                business_id=business.id,
                type__in=Transaction.SALES_MONEY_TYPES,
            ).cleared(),
            'date', start, end)

        if period == 'today':
            trend = hourly_trend(money.values_list('created_at', 'amount'))
        else:
            trend = daily_trend(money.values_list('date', 'amount'), period, start, end)
        return Response({
            'trend': trend,
            'period': period_payload(period, start, end),
        }, status=status.HTTP_200_OK)

    # YEARLY METRICS

    # ALL TIME METRICS

    @action(['GET'], detail=False, url_name='average-order-value', url_path='average-order-value')
    def avg_order_value(self, request):
        if request.method == 'GET':
            business = get_active_business(request)

            if not business:
                return Response({
                    'detail': 'No active business exists. Please contact admin.'
                }, status=status.HTTP_400_BAD_REQUEST)

            avg_order_value = SalesInvoice.objects.average_order_value(
                business.id)
            return Response({
                "avg_order_value": avg_order_value
            }, status=status.HTTP_200_OK)

        return Response({
            "detail": "Method not allowed"
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)


class ReturnedItemsViewSet(PeriodFilterMixin, ModelViewSet):

    filter_backends = [SearchFilter, DjangoFilterBackend]
    filterset_class = ReturnedItemFilter
    search_fields = [
        'id', 'invoice_item__sales_invoice__id', 'invoice_item__product__name', 'invoice_item__product__base__name'
    ]

    def get_queryset(self):
        business = get_active_business(self.request)
        if not business:
            return []

        return ReturnedItem.objects.filter(business_id=business.id).order_by('-created_at')

    # A return has no date of its own, so the period goes by when it was
    # recorded.
    period_lookup = 'created_at__date'

    @action(['GET'], detail=False, url_path='summary', url_name='summary')
    def summary(self, request):
        business = get_active_business(request)
        if not business:
            return Response({
                'detail': 'No active business exists. Please contact admin.'
            }, status=status.HTTP_400_BAD_REQUEST)

        period, start, end = period_from_params(request.query_params)
        figures = self.filter_queryset(self.get_queryset()).aggregate(
            count=Count('id'), quantity=Sum('quantity'))
        return Response({
            'returned_items': figures['count'],
            'returned_quantity': figures['quantity'] or 0,
            'filtered': is_filtered(request),
            'period': period_payload(period, start, end),
        }, status=status.HTTP_200_OK)

    def get_serializer_class(self):
        if self.request.method in ('POST', 'PUT', 'PATCH'):
            return ReturnedItemCreateUpdateSerializer
        return ReturnedItemSerializer

    def get_serializer_context(self):
        business = get_active_business(self.request)
        if not business:
            return {}
        return {
            'business_id': business.id
        }

    @action(['POST'], detail=False, url_path='bulk-delete', url_name='bulk-delete')
    def bulk_delete(self, request):
        returned_item_ids = request.data.get('returned_item_ids', [])
        if not returned_item_ids:
            return Response({
                'detail': 'Bad Request.'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            ReturnedItem.objects.filter(id__in=returned_item_ids).delete()
            return Response({
                'detail': 'Success.'
            }, status=status.HTTP_200_OK)

        except Exception as error:
            print(error)
            return Response({
                'detail': 'Internal Server Error.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(['POST'], detail=True, url_path='sales-invoice-return', url_name='sales-invoice-return')
    def sales_invoice_return(self, request, pk=None):
        try:
            instance: ReturnedItem = self.get_object()

            if instance.is_returned:
                return Response({
                    "detail": "Item already returned"
                }, status=status.HTTP_400_BAD_REQUEST)

            instance.return_type = 'to_invoice'

            invoice_item: SalesInvoiceItem = instance.invoice_item
            invoice_item.returned_quantity -= instance.quantity

            with transaction.atomic():
                instance.save()
                invoice_item.save(update_fields=['returned_quantity'])

            return Response({
                "detail": "OK"
            }, status=status.HTTP_200_OK)
        except Exception as error:
            print(error)
            return Response({
                "detail": "Internal Server Error"
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # Implement properly, discussed with GPT in MarketPro 04. Search Keyword: You adjust quantities.
    @action(['POST'], detail=True, url_path='inventory-return', url_name='inventory-return')
    def inventory_return(self, request, pk=None):
        try:
            instance: ReturnedItem = self.get_object()

            if instance.is_returned:
                return Response({
                    "detail": "Item already returned"
                }, status=status.HTTP_400_BAD_REQUEST)

            instance.return_type = 'to_inventory'

            inventory_item = InventoryItem.objects.get(
                product=instance.invoice_item.product)
            inventory_item.quantity += instance.quantity
            inventory_item.quantity_on_hand += instance.quantity

            with transaction.atomic():
                instance.save()
                inventory_item.save()

            return Response({
                "detail": "OK"
            }, status=status.HTTP_200_OK)
        except Exception as error:
            print(error)
            return Response({
                "detail": "Internal Server Error"
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class ReturnedItemsKPIViewSet(GenericViewSet):

    serializer_class = None

    @action(['GET'], detail=False, url_name='total-returned-items', url_path='total-returned-items')
    def total_returned_items(self, request):
        if request.method == 'GET':
            business = get_active_business(request)

            if not business:
                return Response({
                    'detail': 'No active business exists. Please contact admin.'
                }, status=status.HTTP_400_BAD_REQUEST)

            total_returned_items = ReturnedItem.objects.total_returned_items(
                business.id)
            return Response({
                "total_returned_items": total_returned_items
            }, status=status.HTTP_200_OK)

        return Response({
            "detail": "Method not allowed"
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)


class PurchaseQuotationViewSet(ModelViewSet):

    filter_backends = [SearchFilter, DjangoFilterBackend]
    filterset_class = PurchaseQuotationFilter
    # 'status' and 'created_at' are out: an icontains on one-letter codes and
    # on a timestamp matched almost everything. Status is a filter instead.
    search_fields = [
        'id', 'quotation_no', 'notes', 'items__product__name',
        'items__product__base__name', 'items__supplier__name',
    ]

    def get_queryset(self):

        business = get_active_business(self.request)
        return PurchaseQuotation.objects.filter(business_id=business.id)

    def get_serializer_class(self):

        if self.action == 'create_quotation_and_items':
            return PurchaseQuotationAndItemsCreateSerializer

        if self.request.method == 'POST':
            return PurchaseQuotationCreateSerializer

        return PurchaseQuotationSerializer

    def get_serializer_context(self):

        business = get_active_business(self.request)
        if not business:
            return {}

        return {
            'business_id': business.id,
            'user_id': self.request.user.id
        }

    @action(['POST'], detail=False, url_path='bulk-delete', url_name='bulk-delete')
    def bulk_delete(self, request):
        purchase_quotation_ids = request.data.get('purchase_quotation_ids', [])
        if not purchase_quotation_ids:
            return Response({
                'detail': 'Bad Request.'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            PurchaseQuotation.objects.filter(
                id__in=purchase_quotation_ids).delete()
            return Response({
                'detail': 'Success.'
            }, status=status.HTTP_200_OK)

        except Exception as error:
            print(error)
            return Response({
                'detail': 'Internal Server Error.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(['POST'], detail=False, url_path='create-with-items', url_name='create-with-items')
    def create_quotation_and_items(self, request):
        if request.method == 'POST':
            try:
                business = get_active_business(request)
                if not business:
                    return Response({
                        'detail': 'No active business exists. Please contact admin.'
                    }, status=status.HTTP_400_BAD_REQUEST)

                serializer = PurchaseQuotationAndItemsCreateSerializer(data=request.data, context={
                    'business_id': business.id,
                    'user_id': self.request.user.id,
                })
                serializer.is_valid(raise_exception=True)
                quotation = serializer.save()

                if not quotation:
                    return Response({
                        'detail': 'Bad Request.'
                    }, status=status.HTTP_400_BAD_REQUEST)

                res_serializer = PurchaseQuotationSerializer(quotation)
                return Response(res_serializer.data, status=status.HTTP_201_CREATED)

            except Exception as error:
                print(error)
                return Response({
                    'detail': 'Internal Server Error.'
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(['POST'], detail=True, url_path='update-with-items', url_name='update-with-items')
    def update_quotation_with_items(self, request, pk=None):
        try:
            business = get_active_business(request)
            if not business:
                return Response({
                    'detail': 'No active business exists. Please contact admin.'
                }, status=status.HTTP_400_BAD_REQUEST)

            instance = self.get_object()
            if not instance:
                return Response({
                    'detail': 'Not Found.'
                }, status=status.HTTP_404_NOT_FOUND)

            serializer = PurchaseQuotationAndItemsUpdateSerializer(
                instance, data=request.data, 
                context={
                'business_id': business.id,
            })
            serializer.is_valid(raise_exception=True)
            sales_invoice = serializer.save()

            if not sales_invoice:
                return Response({
                    'detail': 'Bad Request.'
                }, status=status.HTTP_400_BAD_REQUEST)

            res_serializer = SalesInvoiceSerializer(sales_invoice)
            return Response(res_serializer.data, status=status.HTTP_200_OK)

        except Exception as error:
            print(error)
            return Response({
                'detail': 'Internal Server Error.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class PurchaseQuotationItemViewSet(ModelViewSet):
    filter_backends = [SearchFilter, DjangoFilterBackend]
    # These were a copy of the sales invoice items' (sales_invoice__...),
    # parked in a string; these are the quotation item's own.
    filterset_fields = ['supplier', 'is_fulfilled', 'product']
    search_fields = [
        'id', 'product__name', 'product__base__name', 'supplier__name', 'track_code', 'notes',
    ]

    def get_queryset(self):
        return PurchaseQuotationItem.objects.filter(purchase_quotation_id=self.kwargs['purchase_quotation_pk'])

    def get_serializer_class(self):
        method = self.request.method

        if self.action == 'list':
            return SimplePurchaseQuotationItemSerializer

        if method in ('POST', 'PUT', 'PATCH'):
            return PurchaseQuotationItemCreateUpdateSerializer

        return PurchaseQuotationItemSerializer

    def get_serializer_context(self):

        business = get_active_business(self.request)
        if not business:
            return {}
        return {
            'business_id': business.id,
            'purchase_quotation_id': self.kwargs['purchase_quotation_pk'],
        }

    @action(methods=['GET'], detail=True, url_path='toggle-fulfillment', url_name='toggle-fulfillment')
    def toggle_fulfillment(self, request, purchase_quotation_pk=None, pk=None):

        try:
            object = self.get_object()
            object.is_fulfilled = not object.is_fulfilled
            object.save()

            return Response({
                "detail": "OK"
            }, status=status.HTTP_200_OK)
        except Exception as error:
            print(error)
            return Response({
                "detail": "Internal Server Error"
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class InvoiceImageViewSet(ModelViewSet):
    """
    The photos on one invoice: list them, upload one, delete one.

    Base class for the two nested routes below. Which invoice is read from the
    URL and scoped to the active business - not taken from the payload - so a
    photo cannot be attached to another business's invoice by sending its id.

    DRF's default parsers already accept multipart, so no parser_classes are
    needed for the upload.
    """

    serializer_class = InvoiceImageSerializer

    # Set by the two subclasses: the FK column on InvoiceImage, and the URL
    # kwarg the nested router supplies.
    invoice_field = None
    url_kwarg = None
    invoice_model = None

    def get_invoice(self):
        """The parent invoice, or None if it is not this business's."""
        business = get_active_business(self.request)
        if not business:
            return None
        return self.invoice_model.objects.filter(
            id=self.kwargs[self.url_kwarg], business_id=business.id
        ).first()

    def get_queryset(self):
        invoice = self.get_invoice()
        if not invoice:
            return InvoiceImage.objects.none()
        return InvoiceImage.objects.filter(**{self.invoice_field: invoice.id})

    def get_serializer_context(self):
        # request is what turns the stored path into an absolute URL. A context
        # without it replaces DRF's default and every image comes back as a
        # bare /media/... path the frontend cannot load.
        return {'request': self.request}

    def create(self, request, *args, **kwargs):
        invoice = self.get_invoice()
        if not invoice:
            return Response({'detail': 'Not Found.'},
                            status=status.HTTP_404_NOT_FOUND)

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            serializer.save(**{
                self.invoice_field: invoice,
                'uploaded_by': request.user,
            })
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        except Exception as error:
            print(error)
            return Response({'detail': 'Internal Server Error.'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def perform_destroy(self, instance):
        # The file goes with the row. Django stopped deleting files on
        # delete() in 1.3, so an untouched default leaves the storage filling
        # up with photos no invoice references any more.
        stored = instance.image
        instance.delete()
        if stored:
            stored.delete(save=False)


class SalesInvoiceImageViewSet(InvoiceImageViewSet):

    invoice_field = 'sales_invoice'
    url_kwarg = 'sales_invoice_pk'
    invoice_model = SalesInvoice


class PurchaseInvoiceImageViewSet(InvoiceImageViewSet):

    invoice_field = 'purchase_invoice'
    url_kwarg = 'purchase_invoice_pk'
    invoice_model = PurchaseInvoice
