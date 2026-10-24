from datetime import datetime
from django.db.models import Count, Exists, OuterRef, QuerySet, Sum
from django.utils import timezone
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.viewsets import ReadOnlyModelViewSet, ModelViewSet, GenericViewSet, ViewSet
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.filters import SearchFilter
from django_filters.rest_framework import DjangoFilterBackend

from accounts.utils import (
    customer_balance, month_bounds, party_invoiced, supplier_balance,
)
from core.utils import send_marketpro_email
# root -> inventory, the same direction sales/ already uses. inventory.models
# imports root.models, not root.views, so there is no cycle.
from inventory.models import InventoryItem
from .periods import (
    TREND_PERIODS, PeriodFilterMixin, daily_trend, hourly_trend, in_period,
    is_filtered, period_from_params, period_payload,
)
from .utils import get_active_business, whatsapp_number
from .serializers import (
    BusinessCreateSerializer, CategorySerializer, CitySerializer, CustomerSerializer, EmployeeAndUserCreateSerializer, ExpenseSerializer, LocationSerializer,
    BusinessSerializer, ProductAndVariantCreateSerializer, ProductAndVariantUpdateSerializer, ProductVariantTypeSerializer, SimpleCustomerSerializer, SimpleProductSerializer, SupplierSerializer, UnitSerializer,
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
from .list_filters import CustomerFilter, ExpenseFilter, ProductFilter
from .search.engine import search as global_search
from .new_records import (
    add_variants, create_customer, create_product_with_stock, create_supplier, product_answer,
    product_for_editing, supplier_label,
)
from audit.helpers import audited_bulk_update


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
    # Login reads the user's business; data entry may do that and no more.
    allow_backlog_role = {'list:GET', 'retrieve:GET'}

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

            audited_bulk_update(Business, businesses, ['is_active'])

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
    filterset_class = ProductFilter
    # A product is found by its variants too: their names ("White / 1.5 Ton")
    # and SKUs are what is printed on the invoice. Each word may match a
    # different field, so "Royal Fan White" finds the fan with a White
    # variant. SearchFilter de-duplicates across the variants join.
    search_fields = [
        'id', 'name', 'code', 'desc', 'unit__name', 'variants__name', 'variants__sku',
    ]

    @action(['POST'], detail=False, url_path='create-with-stock', url_name='create-with-stock')
    def create_with_stock(self, request):
        """A product typed on an invoice that is not in MarketPro yet: the
        product, its variants and a stock row for each, in one go
        (root/new_records.py). Answers with ids and names."""
        business = get_active_business(request)
        if not business:
            return Response({'detail': 'No active business exists. Please contact admin.'}, status=status.HTTP_400_BAD_REQUEST)
        product, variants = create_product_with_stock(business, request.data)
        return Response(product_answer(product, variants), status=status.HTTP_201_CREATED)

    @action(['GET', 'POST'], detail=True, url_path='variants-and-stock', url_name='variants-and-stock')
    def variants_and_stock(self, request, pk=None):
        """A product an invoice's picker matched by name. GET: its details and
        variants, for the dialog. POST: the dialog's changes - details,
        variants updated, new variants with a stock row each; nothing is
        deleted (root/new_records.py). Answers with the new variants."""
        business = get_active_business(request)
        if not business:
            return Response({'detail': 'No active business exists. Please contact admin.'}, status=status.HTTP_400_BAD_REQUEST)
        if request.method == 'GET':
            return Response(product_for_editing(business, pk))
        product, new_variants = add_variants(business, pk, request.data)
        return Response(product_answer(product, new_variants), status=status.HTTP_200_OK)

    def get_queryset(self):
        business = get_active_business(self.request)
        if not business:
            return []

        return Product.objects.filter(business_id=business.id).order_by('name')

    def get_serializer_class(self):
        
        method = self.request.method

        if self.action == 'list':
            return SimpleProductSerializer

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
        business = get_active_business(self.request)
        if not business:
            # none(), never [] or None: both break the moment anything calls
            # .model or .filter on the result, which is a 500 rather than an
            # empty list.
            return ProductVariant.objects.none()

        queryset = ProductVariant.objects.filter(
            base__business=business).order_by('base__name')

        # ?has_inventory_item=false lists only variants that are not stocked
        # yet. The create-inventory-item screen offers exactly those: picking a
        # variant that already has an item would only create a duplicate, and
        # nothing stops that today.
        wanted = self.request.query_params.get('has_inventory_item')
        if wanted is not None:
            stocked = InventoryItem.objects.filter(
                inventory__business_id=business.id,
                product_id=OuterRef('pk'),
            )
            queryset = queryset.annotate(
                has_inventory_item=Exists(stocked)
            ).filter(
                has_inventory_item=wanted.strip().lower() in ('true', '1', 'yes')
            )

        return queryset

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

    @action(['POST'], detail=False, url_path='create-with-balance', url_name='create-with-balance')
    def create_with_balance(self, request):
        """A supplier typed on an invoice that is not in MarketPro yet, with
        the balance their khaata stood at, if given (root/new_records.py)."""
        business = get_active_business(request)
        if not business:
            return Response({'detail': 'No active business exists. Please contact admin.'}, status=status.HTTP_400_BAD_REQUEST)
        supplier = create_supplier(business, request.data)
        return Response({'id': supplier.id, 'label': supplier_label(supplier)}, status=status.HTTP_201_CREATED)

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

    ### the money picture for one supplier, for the detail screen cards
    @action(['GET'], detail=True, url_path='summary', url_name='summary')
    def summary(self, request, pk=None):

        business = get_active_business(request)
        if not business:
            return Response({
                'detail': 'No active business exists. Please contact admin.'
            }, status=status.HTTP_400_BAD_REQUEST)

        supplier = Supplier.objects.filter(
            id=pk, business_id=business.id).first()
        if not supplier:
            return Response({
                'detail': 'Not Found'
            }, status=status.HTTP_404_NOT_FOUND)

        # Total Business follows the card's period; the balance never does.
        period, start, end = period_from_params(request.query_params)
        try:
            return Response({
                'balance': supplier_balance(supplier, business.id),
                'total_business': round(party_invoiced(
                    business.id, supplier=supplier, date_from=start, date_to=end)),
                'period': period_payload(period, start, end),
                # Reminders go out from this page too, and they must work
                # without accounting access, so the wa.me number rides along.
                'whatsapp': whatsapp_number(supplier.phone),
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
    filterset_class = CustomerFilter
    search_fields = [
        'id', 'name', 'phone', 'email', 'address', 'city__name'
    ]

    @action(['POST'], detail=False, url_path='create-with-balance', url_name='create-with-balance')
    def create_with_balance(self, request):
        """A customer typed on an invoice that is not in MarketPro yet, with
        the balance their khaata stood at, if given (root/new_records.py)."""
        business = get_active_business(request)
        if not business:
            return Response({'detail': 'No active business exists. Please contact admin.'}, status=status.HTTP_400_BAD_REQUEST)
        customer = create_customer(business, request.data)
        return Response({'id': customer.id, 'label': customer.name}, status=status.HTTP_201_CREATED)

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

    ### the money picture for one customer, for the detail screen cards
    @action(['GET'], detail=True, url_path='summary', url_name='summary')
    def summary(self, request, pk=None):

        business = get_active_business(request)
        if not business:
            return Response({
                'detail': 'No active business exists. Please contact admin.'
            }, status=status.HTTP_400_BAD_REQUEST)

        customer = Customer.objects.filter(
            id=pk, business_id=business.id).first()
        if not customer:
            return Response({
                'detail': 'Not Found'
            }, status=status.HTTP_404_NOT_FOUND)

        # Total Business follows the card's period; the balance never does.
        period, start, end = period_from_params(request.query_params)
        try:
            return Response({
                'balance': customer_balance(customer, business.id),
                'total_business': round(party_invoiced(
                    business.id, customer=customer, date_from=start, date_to=end)),
                'period': period_payload(period, start, end),
                # Reminders go out from this page too, and they must work
                # without accounting access, so the wa.me number rides along.
                'whatsapp': whatsapp_number(customer.phone),
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


class ExpenseViewSet(PeriodFilterMixin, ModelViewSet):
    serializer_class = ExpenseSerializer
    filter_backends = [SearchFilter, DjangoFilterBackend]
    filterset_class = ExpenseFilter
    search_fields = [
        'id', 'name', 'category', 'desc', 'amount', 'created_at'
    ]

    def get_queryset(self):
        business = get_active_business(self.request)
        if not business:
            return []

        # Dated, sorted and period-switched by the day the money went out,
        # which is what the transactions list shows for the same expense.
        # created_at only breaks ties between expenses paid on one day.
        return (Expense.objects.filter(business_id=business.id)
                .with_expense_date()
                .select_related('transaction_record__account')
                .order_by('-expense_date', '-created_at'))

    period_lookup = 'expense_date'

    @action(['GET'], detail=False, url_path='summary', url_name='summary')
    def summary(self, request):
        """The Expenses cards, from the rows the table shows."""
        business = get_active_business(request)
        if not business:
            return Response({
                'detail': 'No active business exists. Please contact admin.'
            }, status=status.HTTP_400_BAD_REQUEST)

        period, start, end = period_from_params(request.query_params)
        figures = self.filter_queryset(self.get_queryset()).aggregate(
            count=Count('id'), amount=Sum('amount'))
        return Response({
            'expense_count': figures['count'],
            'expense_amount': round(figures['amount'] or 0),
            'filtered': is_filtered(request),
            'period': period_payload(period, start, end),
        }, status=status.HTTP_200_OK)

    def get_serializer_context(self):
        business = get_active_business(self.request)
        if not business:
            return {}
        return {
            'business_id': business.id
        }

    # An invoice's extra-charge expenses belong to the invoice, which rebuilds
    # them from its charges and payments; deleting one here would only last
    # until the invoice was next touched.
    CHARGE_EXPENSE_REFUSAL = {
        'detail': 'One or more of these expenses is an extra charge on a sales '
                  'invoice. Change or remove the charge on the invoice instead.'
    }

    def destroy(self, request, *args, **kwargs):
        if self.get_object().is_invoice_charge:
            return Response(self.CHARGE_EXPENSE_REFUSAL,
                            status=status.HTTP_400_BAD_REQUEST)
        return super().destroy(request, *args, **kwargs)

    @action(['POST'], detail=False, url_path='bulk-delete', url_name='bulk-delete')
    def bulk_delete(self, request):

        expense_ids = request.data.get('expense_ids', [])
        if not expense_ids:
            return Response({
                'detail': 'Bad Request.'
            }, status=status.HTTP_400_BAD_REQUEST)

        if Expense.objects.filter(
                id__in=expense_ids, sales_invoice__isnull=False).exists():
            return Response(self.CHARGE_EXPENSE_REFUSAL,
                            status=status.HTTP_400_BAD_REQUEST)

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

            # The calendar month, as the card says. This counted every
            # expense the business had ever recorded.
            start, end = month_bounds()
            monthly_total_expenses = (
                Expense.objects.filter(business_id=business.id)
                .with_expense_date()
                .in_range('expense_date', start, end)
                .count())
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

            # The calendar month, not a rolling window.
            #
            # This passed today.day as num_days, which is the day of the month
            # used as a number of days back: one day long on the 1st, and on
            # the 31st reaching into the previous month. It was never the
            # current month.
            # Dated by the day the money went out (with_expense_date).
            start, end = month_bounds()
            monthly_total_expense_amount = Expense.objects.expense_amount_in_range(
                business.id, start, end)
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

    @action(['GET'], detail=False, url_name='expenses-trend', url_path='expenses-trend')
    def expenses_trend(self, request):
        """
        Expenses over the dashboard chart's period: by hour today, by day for
        a week or a month, by week for a quarter, by month for a year. Dated
        by the day the money went out (with_expense_date).
        """
        business = get_active_business(request)
        if not business:
            return Response({
                'detail': 'No active business exists. Please contact admin.'
            }, status=status.HTTP_400_BAD_REQUEST)

        period, start, end = period_from_params(
            request.query_params, default='month', allowed=TREND_PERIODS)
        expenses = in_period(
            Expense.objects.filter(business_id=business.id).with_expense_date(),
            'expense_date', start, end,
        )

        if period == 'today':
            # A paid date has no time of day; the hour is when it was entered.
            trend = hourly_trend(expenses.values_list('created_at', 'amount'))
        else:
            trend = daily_trend(expenses.values_list('expense_date', 'amount'), period, start, end)
        return Response({
            'trend': trend,
            'period': period_payload(period, start, end),
        }, status=status.HTTP_200_OK)


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
    

class GlobalSearchView(APIView):
    """
    GET /global-search/?q=<text> - the header's search box.

    Every type the user may see, five hits each with a total, empty groups
    left out. See root/search for how types are declared and matched.
    """

    def get(self, request):
        business = get_active_business(request)
        if not business:
            return Response({
                'detail': 'No active business exists. Please contact admin.'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            return Response(
                global_search(request, business, request.query_params.get('q', '')),
                status=status.HTTP_200_OK)
        except Exception as error:
            print(f"There was an error performing the search: {error}")
            return Response({'detail': 'Internal Server Error.'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)


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