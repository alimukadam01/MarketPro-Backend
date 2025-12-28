from django.urls import path
from rest_framework.routers import DefaultRouter
from rest_framework_nested.routers import NestedDefaultRouter
from .views import BusinessViewSet, CategoryViewSet, CityViewSet, CustomerViewSet, KeyPerformanceIndicatorsViewSet, LocationViewSet, MultiModelSearchView, SupplierViewSet, UnitViewSet, ProductViewSet

router = DefaultRouter()
router.register('business', BusinessViewSet, basename='business')
router.register('products', ProductViewSet, basename='products')
router.register('cities', CityViewSet)
router.register('units', UnitViewSet)
router.register('categories', CategoryViewSet)
router.register('suppliers', SupplierViewSet, basename='suppliers')
router.register('locations', LocationViewSet, basename='locations')
router.register('customers', CustomerViewSet, basename='customers')
router.register('kpis', KeyPerformanceIndicatorsViewSet, basename='kpis')

urlpatterns = [
    path('search/', MultiModelSearchView.as_view())
] + router.urls