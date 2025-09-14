from rest_framework import status
from rest_framework.viewsets import ModelViewSet
from rest_framework.decorators import action
from rest_framework.response import Response

from root.utils import get_active_business
from .models import PurchaseInvoice, PurchaseInvoiceItem, SalesInvoice, SalesInvoiceItem
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
    SalesInvoiceAndItemsCreateSerializer,
    SalesInvoiceAndItemsUpdateSerializer,
    SalesInvoiceCreateSerializer,
    SalesInvoiceItemCreateSerializer,
    SalesInvoiceItemSerializer,
    SalesInvoiceItemUpdateSerializer,
    SalesInvoiceSerializer,
    SalesInvoiceUpdateSerializer,
    SimplePurchaseInvoiceItemSerializer,
    SimplePurchaseInvoiceSerializer,
    SimpleSalesInvoiceItemSerializer,
    SimpleSalesInvoiceSerializer, 
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
                sales_invoice = serializer.save()

                if not sales_invoice:
                    return Response({
                        'detail': 'Bad Request.'
                    }, status=status.HTTP_400_BAD_REQUEST)
                
                res_serializer = PurchaseInvoiceSerializer(sales_invoice)
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
                      
                serializer = PurchaseInvoiceAndItemsUpdateSerializer(instance, data=request.data, context={
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
                return Response(res_serializer.data, status=status.HTTP_200_OK)
            
            except Exception as error:
                print(error)
                return Response({
                    'detail': 'Internal Server Error.'
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class PurchaseInvoiceItemViewSet(ModelViewSet):
    
    
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