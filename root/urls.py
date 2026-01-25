from django.urls import path
from rest_framework.routers import DefaultRouter
from .views import BusinessViewSet, CategoryViewSet, CityViewSet, CustomerKPIViewSet, CustomerViewSet, ExpenseKPIViewSet, ExpenseViewSet, KeyPerformanceIndicatorsViewSet, LocationKPIViewSet, LocationViewSet, MultiModelSearchView, ProductKPIViewSet, SupplierKPIViewSet, SupplierViewSet, UnitViewSet, ProductViewSet

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

router.register('customer-kpis', CustomerKPIViewSet, basename='customer-kpis')
router.register('product-kpis', ProductKPIViewSet, basename='products-kpis')
router.register('location-kpis', LocationKPIViewSet, basename='locations-kpis')
router.register('supplier-kpis', SupplierKPIViewSet, basename='supplier-kpis')
router.register('expenses-kpis', ExpenseKPIViewSet, basename='expenses-kpis')

urlpatterns = [
    path('search/', MultiModelSearchView.as_view())
] + router.urls