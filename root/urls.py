from django.urls import path
from rest_framework_nested.routers import DefaultRouter, NestedDefaultRouter
from .views import (
    BusinessViewSet, BusinessConfigViewSet, CategoryViewSet, CityViewSet,
    CustomerKPIViewSet, CustomerViewSet, ExpenseKPIViewSet,
    ExpenseViewSet, LocationKPIViewSet,
    LocationViewSet, MultiModelSearchView, ProductKPIViewSet, ProductRelatedVariantViewSet,
    SupplierKPIViewSet, SupplierViewSet, UnitViewSet,
    ProductViewSet, EmailTestingView, ProductVariantViewSet, ProductVariantTypeViewSet,
    EmployeeViewSet, EmployeePermissionsViewSet,
)

router = DefaultRouter()
router.register('business', BusinessViewSet, basename='business')
router.register('products', ProductViewSet, basename='products')
router.register('cities', CityViewSet)
router.register('units', UnitViewSet)
router.register('categories', CategoryViewSet)
router.register('suppliers', SupplierViewSet, basename='suppliers')
router.register('locations', LocationViewSet, basename='locations')
router.register('customers', CustomerViewSet, basename='customers')
router.register('expenses', ExpenseViewSet, basename='expenses')
router.register('product-variant-types', ProductVariantTypeViewSet, basename='product-variant-types')
router.register('product-variants', ProductVariantViewSet, basename='product-variants')

products_router = NestedDefaultRouter(router, 'products', lookup='product')
products_router.register('variants', ProductRelatedVariantViewSet, basename='variants')

business_router = NestedDefaultRouter(router, 'business', lookup='business')
business_router.register('employees', EmployeeViewSet, basename='business-employees')
business_router.register('config', BusinessConfigViewSet, basename='business-config')

employees_router = NestedDefaultRouter(business_router, 'employees', lookup='employee')
employees_router.register('permissions', EmployeePermissionsViewSet, basename='employee-permissions')

router.register('customer-kpis', CustomerKPIViewSet, basename='customer-kpis')
router.register('product-kpis', ProductKPIViewSet, basename='products-kpis')
router.register('location-kpis', LocationKPIViewSet, basename='locations-kpis')
router.register('supplier-kpis', SupplierKPIViewSet, basename='supplier-kpis')
router.register('expenses-kpis', ExpenseKPIViewSet, basename='expenses-kpis')

urlpatterns = [
    path('search/', MultiModelSearchView.as_view()),
    path('test-email/', EmailTestingView.as_view()),
] + router.urls + products_router.urls + business_router.urls + employees_router.urls