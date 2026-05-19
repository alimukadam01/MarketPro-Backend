from datetime import datetime
from django.db.models import QuerySet
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.viewsets import ReadOnlyModelViewSet, ModelViewSet, GenericViewSet, ViewSet
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.filters import SearchFilter
from django_filters.rest_framework import DjangoFilterBackend

from core.utils import send_marketpro_email
from .utils import get_active_business
from .serializers import (
    BusinessCreateSerializer, CategorySerializer, CitySerializer, CustomerSerializer, EmployeeAndUserCreateSerializer, ExpenseSerializer, LocationSerializer,
    BusinessSerializer, ProductAndVariantCreateSerializer, ProductAndVariantUpdateSerializer, ProductVariantTypeSerializer, SimpleCustomerSerializer, SupplierSerializer, UnitSerializer,
    ProductCreateUpdateSerializer, ProductSerializer, ProductVariantCreateSerializer, ProductVariantSerializer,
    EmployeeSerializer, EmployeeCreateSerializer, EmployeeUpdateSerializer,
    EmployeeAccessSerializer, EmployeeAccessCreateSerializer, EmployeeAccessUpdateSerializer,
    BusinessConfigSerializer, BusinessConfigCreateSerializer, BusinessConfigUpdateSerializer,
)
from .models import (
    Business, BusinessConfig, Category, City,
    Customer, Employee, EmployeeAccess, Expense, Location,
    Product, ProductVariant, ProductVariantType, Supplier, Unit,
)
from .filters import GlobalSearch


class CategoryViewSet(ReadOnlyModelViewSet):

    queryset = Category.objects.all().order_by('name')
    serializer_class = CategorySerializer


class CityViewSet(ReadOnlyModelViewSet):

    queryset = City.objects.all().order_by('name')
    serializer_class = CitySerializer


class UnitViewSet(ReadOnlyModelViewSet):

    queryset = Unit.objects.all().order_by('name')
    serializer_class = UnitSerializer


# inject authentication here
class BusinessViewSet(ModelViewSet):

    def get_queryset(self):

        user = self.request.user

        if user and user.is_authenticated:

            owned = Business.objects.filter(owner_id = user.id)
            if not owned:
                try:
                    employee = Employee.objects.get(user = user)
                    return [employee.business]
                
                except Employee.DoesNotExist as error:
                    print(error)
                    return []
            return owned
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


class ProductVariantTypeViewSet(ReadOnlyModelViewSet):

    queryset = ProductVariantType.objects.all()
    serializer_class = ProductVariantTypeSerializer


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

        return Product.objects.filter(business_id=business.id).order_by('name')

    def get_serializer_class(self):
        
        method = self.request.method

        if self.action == 'create_product_and_variants' and method == 'POST':
            return ProductAndVariantCreateSerializer
        
        if self.action == 'update_product_and_variants' and method == 'POST':
            return ProductAndVariantUpdateSerializer

        if method in ('PUT', 'PATCH'):
            return ProductCreateUpdateSerializer
        return ProductSerializer

    def get_serializer_context(self):

        business = get_active_business(self.request)
        if not business:
            return {}

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
    
    @action(['POST'], detail=False, url_path='create-with-variants', url_name='create-with-variants')
    def create_product_and_variants(self, request):

        try:
            business = get_active_business(request)
            if not business:
                return Response({
                    'detail': 'No active business exists. Please contact admin.'
                }, status=status.HTTP_400_BAD_REQUEST)

            serializer = ProductAndVariantCreateSerializer(
                data=request.data, context={
                'business_id': business.id,
            })
            serializer.is_valid(raise_exception=True)
            product = serializer.save()

            if not product:
                return Response({
                    'detail': 'Bad Request.'
                }, status=status.HTTP_400_BAD_REQUEST)

            res_serializer = ProductSerializer(product)
            return Response(res_serializer.data, status=status.HTTP_201_CREATED)

        except Exception as error:
            print(error)
            return Response({
                'detail': 'Internal Server Error.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    @action(['POST'], detail=True, url_path='update-with-variants', url_name='update-with-variants')
    def update_product_and_variants(self, request, pk=None):

        try:
            business = get_active_business(request)
            if not business:
                return Response({
                    'detail': 'No active business exists. Please contact admin.'
                }, status=status.HTTP_400_BAD_REQUEST)

            instance = self.get_object()

            serializer = ProductAndVariantUpdateSerializer(
                data=request.data, 
                instance=instance,
                context={
                'business_id': business.id,
                }
            )
            serializer.is_valid(raise_exception=True)
            product = serializer.save()

            if not product:
                return Response({
                    'detail': 'Bad Request.'
                }, status=status.HTTP_400_BAD_REQUEST)

            res_serializer = ProductSerializer(product)
            return Response(res_serializer.data, status=status.HTTP_201_CREATED)

        except Exception as error:
            print(error)
            return Response({
                'detail': 'Internal Server Error.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class ProductVariantViewSet(ModelViewSet):

    def get_queryset(self):
        try:
            business = get_active_business(self.request)
            if not business:
                return []

            return ProductVariant.objects.filter(base__business=business).order_by('base__name')
        
        except Exception as error:
            return None
        
    def get_serializer_class(self):
        method = self.request.method

        if method == 'POST':
            return ProductVariantCreateSerializer
        return ProductVariantSerializer

    def get_serializer_context(self):
        business = get_active_business(self.request)
        if not business:
            return {}
        
        return {
            'business_id': business.id
        }


class ProductRelatedVariantViewSet(ModelViewSet):

    def get_queryset(self):
        return ProductVariant.objects.filter(product_id = self.kwargs['product_pk'])
    
    def get_serializer_class(self, *args, **kwargs):

        method = self.request.method

        if method == 'POST':
            return ProductVariantCreateSerializer
        return ProductVariantSerializer

    def get_serializer_context(self):
        business = get_active_business(self.request)
        if not business:
            return {}

        return {
            'business_id': business.id,
            'product_id': self.kwargs['product_pk']
        }


class ProductKPIViewSet(GenericViewSet):

    serializer_class = None

    @action(['GET'], detail=False, url_name='total-products', url_path='total-products')
    def total_products(self, request):
        if request.method == 'GET':
            business = get_active_business(request)

            if not business:
                return Response({
                    'detail': 'No active business exists. Please contact admin.'
                }, status=status.HTTP_400_BAD_REQUEST)

            total_products = Product.objects.total_products(business.id)
            return Response({
                "total_products": total_products
            }, status=status.HTTP_200_OK)

        return Response({
            "detail": "Method not allowed"
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)


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

        return Supplier.objects.filter(business_id=business.id).order_by('name')

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


class SupplierKPIViewSet(GenericViewSet):

    serializer_class = None

    @action(['GET'], detail=False, url_name='total-suppliers', url_path='total-suppliers')
    def total_suppliers(self, request):
        if request.method == 'GET':
            business = get_active_business(request)

            if not business:
                return Response({
                    'detail': 'No active business exists. Please contact admin.'
                }, status=status.HTTP_400_BAD_REQUEST)

            total_suppliers = Supplier.objects.total_suppliers(business.id)
            return Response({
                "total_suppliers": total_suppliers
            }, status=status.HTTP_200_OK)

        return Response({
            "detail": "Method not allowed"
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)


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

        return Location.objects.filter(business_id=business.id).order_by('name')

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


class LocationKPIViewSet(GenericViewSet):

    serializer_class = None

    @action(['GET'], detail=False, url_name='total-locations', url_path='total-locations')
    def total_locations(self, request):
        if request.method == 'GET':
            business = get_active_business(request)

            if not business:
                return Response({
                    'detail': 'No active business exists. Please contact admin.'
                }, status=status.HTTP_400_BAD_REQUEST)

            total_locations = Location.objects.total_locations(business.id)
            return Response({
                "total_locations": total_locations
            }, status=status.HTTP_200_OK)

        return Response({
            "detail": "Method not allowed"
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)


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

        return Customer.objects.filter(business_id=business.id).order_by('name')

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


class CustomerKPIViewSet(GenericViewSet):

    serializer_class = None

    @action(['GET'], detail=False, url_name='total-customers', url_path='total-customers')
    def total_customers(self, request):
        if request.method == 'GET':
            business = get_active_business(request)

            if not business:
                return Response({
                    'detail': 'No active business exists. Please contact admin.'
                }, status=status.HTTP_400_BAD_REQUEST)

            total_customers = Customer.objects.total_customers(business.id)
            return Response({
                "total_customers": total_customers
            }, status=status.HTTP_200_OK)

        return Response({
            "detail": "Method not allowed"
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)


class ExpenseViewSet(ModelViewSet):
    serializer_class = ExpenseSerializer
    filter_backends = [SearchFilter, DjangoFilterBackend]
    filterset_fields = ['name', 'desc', 'amount']
    search_fields = [
        'id', 'name', 'desc', 'amount', 'created_at'
    ]

    def get_queryset(self):
        business = get_active_business(self.request)
        if not business:
            return []

        return Expense.objects.filter(business_id=business.id).order_by('-created_at')

    def get_serializer_context(self):
        business = get_active_business(self.request)
        if not business:
            return {}
        return {
            'business_id': business.id
        }

    @action(['POST'], detail=False, url_path='bulk-delete', url_name='bulk-delete')
    def bulk_delete(self, request):

        expense_ids = request.data.get('expense_ids', [])
        if not expense_ids:
            return Response({
                'detail': 'Bad Request.'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            Expense.objects.filter(id__in=expense_ids).delete()
            return Response({
                'detail': 'Success.'
            }, status=status.HTTP_200_OK)

        except Exception as error:
            print(error)
            return Response({
                'detail': 'Internal Server Error.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class ExpenseKPIViewSet(GenericViewSet):

    serializer_class = None

    @action(['GET'], detail=False, url_name='monthly-total-expenses', url_path='monthly-total-expenses')
    def monthly_total_expenses(self, request):
        if request.method == 'GET':
            business = get_active_business(request)

            if not business:
                return Response({
                    'detail': 'No active business exists. Please contact admin.'
                }, status=status.HTTP_400_BAD_REQUEST)

            monthly_total_expenses = Expense.objects.total_expenses(business.id)
            return Response({
                "monthly_total_expenses": monthly_total_expenses
            }, status=status.HTTP_200_OK)

        return Response({
            "detail": "Method not allowed"
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    @action(['GET'], detail=False, url_name='monthly-total-expense-amount', url_path='monthly-total-expense-amount')
    def monthly_total_expense_amount(self, request):
        if request.method == 'GET':
            business = get_active_business(request)

            if not business:
                return Response({
                    'detail': 'No active business exists. Please contact admin.'
                }, status=status.HTTP_400_BAD_REQUEST)

            today = datetime.today()
            monthly_total_expense_amount = Expense.objects.total_expense_amount(business.id, num_days=today.day)
            return Response({
                "monthly_total_expense_amount": monthly_total_expense_amount
            }, status=status.HTTP_200_OK)

        return Response({
            "detail": "Method not allowed"
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    @action(['GET'], detail=False, url_name='monthly-expenses-trend', url_path='monthly-expenses-trend')
    def monthly_expenses_trend(self, request):

        if request.method == 'GET':

            business = get_active_business(request)
            if not business:
                return Response({
                    'detail': 'No active business exists. Please contact admin.'
                }, status=status.HTTP_400_BAD_REQUEST)

            total_expenses = Expense.objects.monthly_expenses_trend(business.id)
            return Response({
                "monthly_expenses_trend": total_expenses
            }, status=status.HTTP_200_OK)

        return Response({
            "detail": "Method not allowed"
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)


class MultiModelSearchView(APIView):

    def get(self, request):

        business = get_active_business(request)
        if not business:
            return Response({
                'detail': 'No active business exists. Please contact admin.'
            }, status=status.HTTP_400_BAD_REQUEST)

        query = request.query_params.get("search", "")
        if not query or not query.strip():
            return Response({"detail": "Query parameter 'search' is required."}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            results = GlobalSearch.search(query, business.id)
        except Exception as error:
            print(f"There was an error performing the search: {error}")
            return Response({"detail": "Internal Server Error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
        return Response(results, status=status.HTTP_200_OK)
    

class KeyPerformanceIndicatorsViewSet(ViewSet):

    pass   


class EmailTestingView(APIView):

    def get(self, request):

        business = get_active_business(request)
        if not business:
            return Response({
                'detail': 'No active business exists. Please contact admin.'
            }, status=status.HTTP_400_BAD_REQUEST)


        user = self.request.user
        try:
            send_marketpro_email(
                "Welcome to Market Pro!", 
                self.request.user.email, 
                'emails/welcome.html', {}
            )
        except:
            return Response({"detail": "Internal Server Error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({"detail": "OK"}, status=status.HTTP_200_OK)


class BusinessConfigViewSet(ModelViewSet):

    def get_queryset(self):
        return BusinessConfig.objects.filter(business_id=self.kwargs['business_pk'])

    def get_serializer_class(self):
        method = self.request.method
        if method == 'POST':
            return BusinessConfigCreateSerializer
        if method in ('PUT', 'PATCH'):
            return BusinessConfigUpdateSerializer
        return BusinessConfigSerializer

    def get_serializer_context(self):
        return {
            'business_id': self.kwargs['business_pk'],
        }


class EmployeeViewSet(ModelViewSet):

    filter_backends = [SearchFilter]
    search_fields = ['user__email', 'user__first_name', 'user__last_name']

    def get_queryset(self):
        return Employee.objects.filter(
            business_id=self.kwargs['business_pk']
        ).order_by('-created_at')

    def get_serializer_class(self):
        method = self.request.method

        if self.action == 'create_user_and_employee':
            return EmployeeAndUserCreateSerializer
        
        if method in ('PUT', 'PATCH'):
            return EmployeeUpdateSerializer
        return EmployeeSerializer

    def get_serializer_context(self):
        business = get_active_business(self.request)
        if not business:
            return {}
        return {
            'business_id': business.id,
            'created_by_id': self.request.user.id,
        }
    
    @action(['POST'], False, url_path='create-with-user', url_name='create-with-user')
    def create_user_and_employee(self, request, business_pk):
        try:

            business = get_active_business(self.request)
            if not business:
                return Response({
                    'detail': 'Bad Request'
                }, status=status.HTTP_400_BAD_REQUEST)

            serializer = self.get_serializer(
                data=request.data, 
                context={
                    'business_id': business.id,
                    'created_by_id': self.request.user.id
                }
            )
            serializer.is_valid(raise_exception=True)
            employee = serializer.save()
            
            if not employee:
                return Response({
                    'detail': 'Bad Request'
                }, status=status.HTTP_400_BAD_REQUEST)

            response_serializer = EmployeeSerializer(employee)
            return Response(response_serializer.data, status=status.HTTP_201_CREATED)
        except Exception as error:
            print(error)
            return Response({
                'detail': 'Internal Server Error'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class EmployeePermissionsViewSet(ModelViewSet):

    def get_queryset(self):
        return EmployeeAccess.objects.filter(employee_id=self.kwargs['employee_pk'])

    def get_serializer_class(self):
        method = self.request.method
        if method == 'POST':
            return EmployeeAccessCreateSerializer
        if method in ('PUT', 'PATCH'):
            return EmployeeAccessUpdateSerializer
        return EmployeeAccessSerializer

    def get_serializer_context(self):
        return {
            'employee_id': self.kwargs['employee_pk'],
        }