from calendar import monthrange
from datetime import datetime

from django.db import transaction

from rest_framework import status
from rest_framework.viewsets import GenericViewSet, ModelViewSet, ViewSet
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.filters import SearchFilter
from django_filters.rest_framework import DjangoFilterBackend

from root.utils import get_active_business
from .models import PurchaseInvoice, PurchaseInvoiceItem, ReturnedItem, SalesInvoice, SalesInvoiceItem
from .serializers import (
    PurchaseInvoiceAndItemsCreateSerializer,
    PurchaseInvoiceAndItemsUpdateSerializer,
    PurchaseInvoiceCreateSerializer,
    PurchaseInvoiceItemCreateSerializer,
    PurchaseInvoiceItemSerializer,
    PurchaseInvoiceItemUpdateSerializer,
    PurchaseInvoiceUpdateSerializer,
    PurchaseInvoiceSerializer,
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
    SimplePurchaseInvoiceItemSerializer,
    SimplePurchaseInvoiceSerializer,
    SimpleSalesInvoiceItemSerializer,
    SimpleSalesInvoiceSerializer,
    GenerateInvoiceSerializer
)

# Create your views here.


class PurchaseInvoiceViewSet(ModelViewSet):

    filter_backends = [SearchFilter, DjangoFilterBackend]
    filterset_fields = ['supplier__name', 'status', 'payment_status', 'sub_total', 'total', 'goods_received']
    search_fields = [
        'id', 'invoice_number', 'supplier__name', 'status', 'payment_status',
        'sub_total', 'total', 'amount_paid', 'goods_received', 'delivery', 'notes'
    ]

    def get_queryset(self):
        business = get_active_business(self.request)
        if not business:
            return []

        return PurchaseInvoice.objects.filter(business_id=business.id).order_by('-created_at')

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

            business = get_active_business(self.request)
            if not business:
                return Response({
                    'detail': 'Not Found.'
                }, status=status.HTTP_404_NOT_FOUND)

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
        if request.method == 'POST':
            try:
                business = get_active_business(self.request)
                if not business:
                    return Response({
                        'detail': 'Not Found.'
                    }, status=status.HTTP_404_NOT_FOUND)

                serializer = PurchaseInvoiceAndItemsCreateSerializer(data=request.data, context={
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
    @transaction.atomic()
    def update_invoice_and_items(self, request, pk=None):
        if request.method == 'POST':
            # try:
            business = get_active_business(self.request)
            if not business:
                return Response({
                    'detail': 'Not Found.'
                }, status=status.HTTP_404_NOT_FOUND)

            instance = self.get_object()
            if not instance:
                return Response({
                    'detail': 'Not Found.'
                }, status=status.HTTP_404_NOT_FOUND)

            existing_items = instance.invoice_items.all()
            existing_items_map = {
                item.id: item for item in existing_items
            }
            existing_item_ids = set(
                existing_items.values_list('id', flat=True))

            updated_items, new_items = [], []
            items = request.data.pop('items', None)
            if not items:
                return Response({
                    'detail': 'Bad Request.'
                }, status=status.HTTP_400_BAD_REQUEST)

            with transaction.atomic():

                serializer = PurchaseInvoiceUpdateSerializer(instance, data=request.data, context={
                    'business_id': business.id,
                    'user_id': self.request.user.id
                })
                serializer.is_valid(raise_exception=True)
                updated_purchase_invoice = serializer.update(
                    instance, serializer.validated_data)

                for item in items:
                    if item['id'] in existing_item_ids:
                        invoice_item = existing_items_map.get(item['id'])
                        invoice_item.product_id = item['product_id']
                        invoice_item.quantity = item['quantity']
                        invoice_item.unit_cost = item['unit_cost']
                        updated_items.append(invoice_item)
                    else:
                        new_items.append(PurchaseInvoiceItem(
                            business_id=business.id,
                            purchase_invoice=updated_purchase_invoice,
                            product_id=item['product_id'],
                            quantity=item['quantity'],
                            unit_cost=item['unit_cost']
                        ))

                if updated_items:
                    PurchaseInvoiceItem.objects.bulk_update(
                        updated_items,
                        ['product_id', 'quantity', 'unit_cost']
                    )

                if new_items:
                    for item in new_items:
                        items_create_serializer = PurchaseInvoiceItemCreateSerializer(data=item, context={
                            'business_id': business.id,
                            'purchase_invoice_id': updated_purchase_invoice.id
                        })
                        items_create_serializer.is_valid(raise_exception=True)
                        items_create_serializer.save()

                updated_ids = [item.id for item in updated_items]
                existing_items = existing_items.exclude(id__in=updated_ids)
                existing_items.delete()

                updated_purchase_invoice.save()

            res_serializer = PurchaseInvoiceSerializer(
                updated_purchase_invoice)
            return Response(res_serializer.data, status=status.HTTP_200_OK)

            # except Exception as error:
            #     print(error)
            #     return Response({
            #         'detail': 'Internal Server Error.'
            #     }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class PurchaseInvoiceItemViewSet(ModelViewSet):

    filter_backends = [SearchFilter, DjangoFilterBackend]
    filterset_fields = [
        'purchase_invoice__id', 'purchase_invoice__invoice_number', 'is_restocked', 'is_partially_restocked'
        'product__name', 'track_code', 'unit_cost', 'quantity_received',
    ]
    search_fields = [
        'id', 'purchase_invoice__id', 'purchase_invoice__invoice_number', 'product__name', 
        'track_code', 'notes', 'unit_cost', 'quantity_received'
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


class PurchasesKPIViewSet(GenericViewSet):

    ### MONTHLY METRICS
    @action(['GET'], detail=False, url_name='monthly-total-purchases', url_path='monthly-total-purchases')
    def monthly_total_purchases(self, request):

        if request.method == 'GET':

            business_id = get_active_business(request)
            if not business_id:
                return Response({
                    'detail': 'Unauthorized'
                }, status=status.HTTP_401_UNAUTHORIZED)

            today = datetime.today()
            total_purchases = PurchaseInvoice.objects.total_purchases(business_id, today.day)
            return Response({
                "total_purchases": total_purchases
            }, status=status.HTTP_200_OK)
        
        return Response({
            "detail": "Method not allowed"
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    @action(['GET'], detail=False, url_name='monthly-total-purchase-invoices', url_path='monthly-total-purchase-invoices')
    def monthly_total_invoices(self, request):
        if request.method == 'GET':

            business_id = get_active_business(request).id
            if not business_id:
                return Response({
                    'detail': 'Unauthorized'
                }, status=status.HTTP_401_UNAUTHORIZED)

            today = datetime.today()
            total_invoices = PurchaseInvoice.objects.total_invoices(business_id, today.day)
            return Response({
                "total_invoices": total_invoices
            }, status=status.HTTP_200_OK)
        
        return Response({
            "detail": "Method not allowed"
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    ### All TIME METRICS
    @action(['GET'], detail=False, url_name='total-purchases', url_path='total-purchases')
    def total_purchases(self, request):
        if request.method == 'GET':
            business_id = get_active_business(request).id
            
            if not business_id:
                return Response({
                    'detail': 'Unauthorized'
                }, status=status.HTTP_401_UNAUTHORIZED)

            total_purchases = PurchaseInvoice.objects.total_purchases(business_id)
            return Response({
                "total_purchases": total_purchases
            }, status=status.HTTP_200_OK)
        
        return Response({
            "detail": "Method not allowed"
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    @action(['GET'], detail=False, url_name='total-pending-invoices', url_path='total-pending-invoices')
    def total_pending_invoices(self, request):
        if request.method == 'GET':

            business_id = get_active_business(request).id
            if not business_id:
                return Response({
                    'detail': 'Unauthorized'
                }, status=status.HTTP_401_UNAUTHORIZED)

            total_invoices = PurchaseInvoice.objects.total_pending_invoices(business_id)
            return Response({
                "total_invoices": total_invoices
            }, status=status.HTTP_200_OK)
        
        return Response({
            "detail": "Method not allowed"
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)
    
    @action(['GET'], detail=False, url_name='total-pending-payment', url_path='total-pending-payment')
    def total_pending_paymnet(self, request):
        if request.method == 'GET':

            business_id = get_active_business(request).id
            if not business_id:
                return Response({
                    'detail': 'Unauthorized'
                }, status=status.HTTP_401_UNAUTHORIZED)

            total_payment = PurchaseInvoice.objects.total_pending_payment(business_id)
            return Response({
                "total_payment": total_payment
            }, status=status.HTTP_200_OK)
        
        return Response({
            "detail": "Method not allowed"
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)


class SalesInvoiceViewSet(ModelViewSet):

    filter_backends = [SearchFilter, DjangoFilterBackend]
    filterset_fields = ['customer__name', 'status', 'payment_status', 'sub_total', 'total', 'is_deducted', 'is_partially_deducted']
    search_fields = [
        'id', 'invoice_number', 'customer__name', 'status', 'payment_status', 'sub_total', 'total', 'discount', 'tax', 'notes', 'created_by__email'
    ]

    def get_queryset(self):
        business = get_active_business(self.request)
        if not business:
            return []

        return SalesInvoice.objects.filter(business_id=business.id).order_by("-created_at")

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
                business = get_active_business(self.request)
                if not business:
                    return Response({
                        'detail': 'Not Found.'
                    }, status=status.HTTP_404_NOT_FOUND)

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
                business = get_active_business(self.request)
                if not business:
                    return Response({
                        'detail': 'Not Found.'
                    }, status=status.HTTP_404_NOT_FOUND)

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


class SalesKPIViewSet(GenericViewSet):

    queryset = []
    serializer_class = None

    ### DAILY METRICS
    @action(['GET'], detail=False, url_name='daily-total-sales', url_path='daily-total-sales')
    def daily_total_sales(self, request):
        if request.method == 'GET':

            business_id = get_active_business(request)
            if not business_id:
                return Response({
                    'detail': 'Unauthorized'
                }, status=status.HTTP_401_UNAUTHORIZED)

            total_sales = SalesInvoice.objects.total_sales(business_id, 1)
            return Response({
                "total_daily_sales": total_sales
            }, status=status.HTTP_200_OK)
        
        return Response({
            "detail": "Method not allowed"
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    @action(['GET'], detail=False, url_name='daily-total-sales-invoices', url_path='daily-total-sales-invoices')
    def daily_total_invoices(self, request):
        if request.method == 'GET':

            business_id = get_active_business(request).id
            if not business_id:
                return Response({
                    'detail': 'Unauthorized'
                }, status=status.HTTP_401_UNAUTHORIZED)

            total_invoices = SalesInvoice.objects.total_invoices(business_id, 1)
            return Response({
                "total_invoices": total_invoices
            }, status=status.HTTP_200_OK)
        
        return Response({
            "detail": "Method not allowed"
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)
    
    @action(['GET'], detail=False, url_name='recent-sales', url_path='recent-sales')
    def recent_sales(self, request):
        if request.method == 'GET':
            business_id = get_active_business(request)
            
            if not business_id:
                return Response({
                    'detail': 'Unauthorized'
                }, status=status.HTTP_401_UNAUTHORIZED)

            recent_sales = SalesInvoice.objects.recent_sales(business_id)
            return Response({
                "recent_sales": recent_sales
            }, status=status.HTTP_200_OK)
        
        return Response({
            "detail": "Method not allowed"
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    @action(methods=['GET'], detail=False, url_name='daily-total-items', url_path='daily-total-items')
    def daily_total_items(self, request):
        if request.method == "GET":
            business_id = get_active_business(request)
            if not business_id:
                return Response({
                    'detail': 'Unauthorized'
                }, status=status.HTTP_401_UNAUTHORIZED)

            total_items = SalesInvoiceItem.objects.total_items_sold(business_id, 1)
            return Response({
                "total_items": total_items
            }, status=status.HTTP_200_OK)
        
        return Response({
            "detail": "Method not allowed"
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    ### MONTHLY METRICS
    @action(['GET'], detail=False, url_name='monthly-total-sales', url_path='monthly-total-sales')
    def monthly_total_sales(self, request):

        if request.method == 'GET':

            business_id = get_active_business(request)
            if not business_id:
                return Response({
                    'detail': 'Unauthorized'
                }, status=status.HTTP_401_UNAUTHORIZED)

            today = datetime.today()
            total_days_month = monthrange(today.year, today.month)[1]
            days = total_days_month - today.day
            total_sales = SalesInvoice.objects.total_sales(business_id, days)
            return Response({
                "total_monthly_sales": total_sales
            }, status=status.HTTP_200_OK)
        
        return Response({
            "detail": "Method not allowed"
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    @action(['GET'], detail=False, url_name='monthly-sales-trend', url_path='monthly-sales-trend')
    def monthly_sales_trend(self, request):

        if request.method == 'GET':

            business_id = get_active_business(request)
            if not business_id:
                return Response({
                    'detail': 'Unauthorized'
                }, status=status.HTTP_401_UNAUTHORIZED)

            total_sales = SalesInvoice.objects.monthly_sales_trend(business_id)
            return Response({
                "monthly_sales_trend": total_sales
            }, status=status.HTTP_200_OK)
        
        return Response({
            "detail": "Method not allowed"
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    ### YEARLY METRICS


    ### ALL TIME METRICS
    @action(['GET'], detail=False, url_name='average-order-value', url_path='average-order-value')
    def avg_order_value(self, request):
        if request.method == 'GET':
            business_id = get_active_business(request).id
            
            if not business_id:
                return Response({
                    'detail': 'Unauthorized'
                }, status=status.HTTP_401_UNAUTHORIZED)

            avg_order_value = SalesInvoice.objects.average_order_value(business_id)
            return Response({
                "avg_order_value": avg_order_value
            }, status=status.HTTP_200_OK)
        
        return Response({
            "detail": "Method not allowed"
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)


class SalesInvoiceItemViewSet(ModelViewSet):

    filter_backends = [SearchFilter, DjangoFilterBackend]
    filterset_fields = [
        'sales_invoice__id', 'sales_invoice__invoice_number', 'is_deducted', 'is_partially_deducted',
        'product__name', 'track_code', 'unit_price', 'quantity_received'
    ]
    search_fields = [
       'id', 'sales_invoice__id', 'sales_invoice__invoice_number', 'product__name', 'track_code', 'quantity_received', 'unit_price', 'discount'
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
            business = get_active_business(self.request)
            if not business:
                return Response({
                    'detail': 'Not Found.'
                }, status=status.HTTP_404_NOT_FOUND)

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


class ReturnedItemsViewSet(ModelViewSet):

    filter_backends = [SearchFilter, DjangoFilterBackend]
    filterset_fields = [
        'invoice_item__sales_invoice__id', 'invoice_item__product__name', 'quantity'
    ]
    search_fields = [
       'id', 'invoice_item__sales_invoice__id', 'invoice_item__product__name'
    ]

    def get_queryset(self):
        business = get_active_business(self.request)
        if not business:
            return []

        return ReturnedItem.objects.filter(business_id=business.id)
    
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
        

class ReturnedItemsKPIViewSet(GenericViewSet):

    serializer_class = None

    @action(['GET'], detail=False, url_name='total-returned-items', url_path='total-returned-items')
    def total_returned_items(self, request):
        if request.method == 'GET':
            business_id = get_active_business(request).id
            
            if not business_id:
                return Response({
                    'detail': 'Unauthorized'
                }, status=status.HTTP_401_UNAUTHORIZED)

            total_returned_items = ReturnedItem.objects.total_returned_items(business_id)
            return Response({
                "total_returned_items": total_returned_items
            }, status=status.HTTP_200_OK)
        
        return Response({
            "detail": "Method not allowed"
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)
    