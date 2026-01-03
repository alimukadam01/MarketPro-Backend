from calendar import monthrange
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.viewsets import ReadOnlyModelViewSet, ModelViewSet, ViewSet
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.filters import SearchFilter
from django_filters.rest_framework import DjangoFilterBackend

from inventory.models import Inventory, InventoryItem
from inventory.serializers import InventoryItemSerializer
from sales.models import PurchaseInvoice, PurchaseInvoiceItem, ReturnedItem, SalesInvoice, SalesInvoiceItem
from sales.serializers import PurchaseInvoiceItemSerializer, PurchaseInvoiceSerializer, ReturnedItemSerializer, SalesInvoiceItemSerializer, SalesInvoiceSerializer

from .utils import get_active_business, build_search_q, serialize_queryset
from .serializers import (
    BusinessCreateSerializer, CategorySerializer, CitySerializer, CustomerSerializer, LocationSerializer, SimpleCustomerSerializer, SupplierSerializer, UnitSerializer,
    BusinessSerializer,
    ProductCreateUpdateSerializer, ProductSerializer
)
from .models import Business, Category, City, Customer, Location, Product, Supplier, Unit
from .filters import GlobalSearch

class CategoryViewSet(ReadOnlyModelViewSet):

    queryset = Category.objects.all()
    serializer_class = CategorySerializer

    @action(['GET'], False)
    def view_user(self, request):
        return Response({
            'detail': 'OK'
        }, status=status.HTTP_200_OK)


class CityViewSet(ReadOnlyModelViewSet):

    queryset = City.objects.all()
    serializer_class = CitySerializer


class UnitViewSet(ReadOnlyModelViewSet):

    queryset = Unit.objects.all()
    serializer_class = UnitSerializer


# inject authentication here
class BusinessViewSet(ModelViewSet):

    def get_queryset(self):

        user = self.request.user

        if user and user.is_authenticated:
            return Business.objects.filter(owner_id=user.id)
        return []

    def get_serializer_class(self):

        method = self.request.method

        if method == 'POST':
            return BusinessCreateSerializer
        return BusinessSerializer

    def get_serializer_context(self):
        return {
            'owner_id': self.request.user.id
        }

    @action(['POST'], detail=True)
    def activate(self, request, pk=None):

        try:
            businesses = Business.objects.filter(owner_id=self.request.user.id)
            if not businesses:
                return Response({
                    "detail": "Not Found"
                }, status=status.HTTP_404_NOT_FOUND)

            for business in businesses:
                business.is_active = (business.id == int(self.kwargs['pk']))

            Business.objects.bulk_update(businesses, ['is_active'])

            return Response({
                "detail": "OK"
            }, status=status.HTTP_200_OK)

        except Exception as error:
            print(error)
            return Response({
                "detail": "Internal Server Error"
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class ProductViewSet(ModelViewSet):

    filter_backends = [SearchFilter, DjangoFilterBackend]
    filterset_fields = ['unit__name', 'is_active']
    search_fields = [
        'id', 'name', 'desc', 'unit__name'
    ]

    def get_queryset(self):
        business = get_active_business(self.request)
        if not business:
            return []

        return Product.objects.filter(business_id=business.id)

    def get_serializer_class(self):

        if self.request.method in ('POST', 'PUT', 'PATCH'):
            return ProductCreateUpdateSerializer
        return ProductSerializer

    def get_serializer_context(self):

        business = get_active_business(self.request)
        if not business:
            return None

        return {
            "business_id": business.id
        }

    @action(['POST'], detail=False, url_path='bulk-delete', url_name='bulk-delete')
    def bulk_delete(self, request):

        product_ids = request.data.get('product_ids', [])
        if not product_ids:
            return Response({
                'detail': 'Bad Request.'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            Product.objects.filter(id__in=product_ids).delete()
            return Response({
                'detail': 'Success.'
            }, status=status.HTTP_200_OK)

        except Exception as error:
            print(error)
            return Response({
                'detail': 'Internal Server Error.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class SupplierViewSet(ModelViewSet):

    filter_backends = [SearchFilter]
    search_fields = [
        'id', 'name', 'business_name', 'phone', 'email', 'notes'
    ]

    serializer_class = SupplierSerializer

    def get_queryset(self):
        business = get_active_business(self.request)
        if not business:
            return []

        return Supplier.objects.filter(business_id=business.id)

    def get_serializer_context(self):
        business = get_active_business(self.request)
        if not business:
            return {}

        return {
            'business_id': business.id,
        }

    @action(['POST'], detail=False, url_path='bulk-delete', url_name='bulk-delete')
    def bulk_delete(self, request):

        supplier_ids = request.data.get('supplier_ids', [])
        if not supplier_ids:
            return Response({
                'detail': 'Bad Request.'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            Supplier.objects.filter(id__in=supplier_ids).delete()
            return Response({
                'detail': 'Success.'
            }, status=status.HTTP_200_OK)

        except Exception as error:
            print(error)
            return Response({
                'detail': 'Internal Server Error.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class LocationViewSet(ModelViewSet):

    filter_backends = [SearchFilter]
    search_fields = [
        'id', 'name', 'address'
    ]

    serializer_class = LocationSerializer

    def get_queryset(self):

        business = get_active_business(self.request)
        if not business:
            return []

        return Location.objects.filter(business_id=business.id)

    def get_serializer_context(self):

        business = get_active_business(self.request)
        if not business:
            return {}

        return {
            'business_id': business.id
        }

    @action(['POST'], detail=False, url_path='bulk-delete', url_name='bulk-delete')
    def bulk_delete(self, request):

        location_ids = request.data.get('location_ids', [])
        if not location_ids:
            return Response({
                'detail': 'Bad Request.'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            Location.objects.filter(id__in=location_ids).delete()
            return Response({
                'detail': 'Success.'
            }, status=status.HTTP_200_OK)

        except Exception as error:
            print(error)
            return Response({
                'detail': 'Internal Server Error.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class CustomerViewSet(ModelViewSet):

    filter_backends = [SearchFilter, DjangoFilterBackend]
    filterset_fields = ['city__name']
    search_fields = [
        'id', 'name', 'phone', 'email', 'address', 'city__name'
    ]

    def get_queryset(self):
        business = get_active_business(self.request)
        if not business:
            return []

        return Customer.objects.filter(business_id=business.id)

    def get_serializer_class(self):
        if self.action == 'list':
            return SimpleCustomerSerializer

        return CustomerSerializer

    def get_serializer_context(self):
        business = get_active_business(self.request)
        if not business:
            return {}
        return {
            'business_id': business.id
        }

    @action(['POST'], detail=False, url_path='bulk-delete', url_name='bulk-delete')
    def bulk_delete(self, request):

        customer_ids = request.data.get('customer_ids', [])
        if not customer_ids:
            return Response({
                'detail': 'Bad Request.'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            Customer.objects.filter(id__in=customer_ids).delete()
            return Response({
                'detail': 'Success.'
            }, status=status.HTTP_200_OK)

        except Exception as error:
            print(error)
            return Response({
                'detail': 'Internal Server Error.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)      


class MultiModelSearchView(APIView):

    def get(self, request):
        
        query = request.query_params.get("search", "")
        if not query or not query.strip():
            return Response({"detail": "Query parameter 'search' is required."}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            results = GlobalSearch.search(query)
        except Exception as error:
            print(f"There was an error performing the search: {error}")
            return Response({"detail": "Internal Server Error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
        return Response(results, status=status.HTTP_200_OK)
    

class KeyPerformanceIndicatorsViewSet(ViewSet):

    @action(['GET'], detail=False, url_name='monthly-total-sales', url_path='monthly-total-sales')
    def monthly_total_sales(self, request):

        if request.method == 'GET':

            business_id = get_active_business(request)
            if not business_id:
                return Response({
                    'detail': 'Unauthorized'
                }, status=status.HTTP_401_UNAUTHORIZED)

            total_sales = SalesInvoice.objects.total_sales(business_id, 30)
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
    
    @action(['GET'], detail=False, url_name='total-inventory-value', url_path='total-inventory-value')
    def total_inventory_value(self, request):
        if request.method == 'GET':
            business_id = get_active_business(request).id
            
            if not business_id:
                return Response({
                    'detail': 'Unauthorized'
                }, status=status.HTTP_401_UNAUTHORIZED)

            total_inventory_value = Inventory.objects.total_inventory_value(business_id)
            return Response({
                "total_inventory_value": total_inventory_value
            }, status=status.HTTP_200_OK)
        
        return Response({
            "detail": "Method not allowed"
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)
    
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