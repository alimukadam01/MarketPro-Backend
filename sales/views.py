from rest_framework import status
from rest_framework.viewsets import ModelViewSet
from rest_framework.decorators import action
from rest_framework.response import Response

from root.utils import get_active_business
from .models import PurchaseInvoice, PurchaseInvoiceItem, SalesInvoice, SalesInvoiceItem
from .serializers import (
    PurchaseInvoiceCreateSerializer,
    PurchaseInvoiceItemSerializer, 
    PurchaseInvoiceUpdateSerializer,
    PurchaseInvoiceSerializer,
    RestockSerializer,
    SalesInvoiceCreateSerializer,
    SalesInvoiceItemSerializer,
    SalesInvoiceSerializer,
    SalesInvoiceUpdateSerializer,
    SimplePurchaseInvoiceItemSerializer,
    SimpleSalesInvoiceItemSerializer, 
)

# Create your views here.


class PurchaseInvoiceViewSet(ModelViewSet):

    def get_queryset(self):
        business = get_active_business(self.request)
        if not business:
            return []
        
        return PurchaseInvoice.objects.filter(business_id = business.id)

    def get_serializer_class(self):
        method = self.request.method

        if self.action == 'restock' and method == 'POST':
            return RestockSerializer

        if method == 'POST':
            return PurchaseInvoiceCreateSerializer
        elif method in ('PUT', 'PATCH'):
            return PurchaseInvoiceUpdateSerializer
        
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


class PurchaseInvoiceItemViewSet(ModelViewSet):
    
    
    def get_serializer_class(self):
        
        if self.action == 'list':
            return SimplePurchaseInvoiceItemSerializer
        
        return PurchaseInvoiceItemSerializer

    def get_queryset(self):
        return PurchaseInvoiceItem.objects.filter(
            purchase_invoice_id = self.kwargs['purchase_invoice_pk']
        )
    

    def get_serializer_context(self):

        business = get_active_business(self.request)
        if not business:
            return {}
        return {
            'purchase_invoice_id': self.kwargs['purchase_invoice_pk'],
            'business_id': business.id
        }


class SalesInvoiceViewSet(ModelViewSet):

    def get_queryset(self):
        business = get_active_business(self.request)
        if not business:
            return []
        
        return SalesInvoice.objects.filter(business_id = business.id)
    
    def get_serializer_class(self):
        method = self.request.method

        if method == 'POST':
            return SalesInvoiceCreateSerializer
        elif method in ('PUT', 'PATCH'):
            return SalesInvoiceUpdateSerializer
        
        return SalesInvoiceSerializer
    
    def get_serializer_context(self):
        business = get_active_business(self.request)
        if not business:
            return {}
        return {
            'business_id': business.id,
            'user_id': self.request.user.id
        }
    

class SalesInvoiceItemViewSet(ModelViewSet):

    def get_queryset(self):
        business = get_active_business(self.request)
        if not business:
            return []
        
        return SalesInvoiceItem.objects.filter(business_id=business.id, sales_invoice_id=self.kwargs['sales_invoice_pk'])
    

    def get_serializer_class(self):
        method = self.request.method

        if self.action == 'list':
            return SimpleSalesInvoiceItemSerializer
        
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