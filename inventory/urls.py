from rest_framework.routers import DefaultRouter
from rest_framework_nested.routers import NestedDefaultRouter
from .views import InventoryItemsViewSet, InventoryViewSet, InventoryKPIViewSet

router = DefaultRouter()
router.register('inventory', InventoryViewSet, basename='inventory')
router.register('inventory-kpis', InventoryKPIViewSet, basename='inventory-kpis')

inventory_router = NestedDefaultRouter(router, 'inventory', lookup='inventory')
inventory_router.register('items', InventoryItemsViewSet, basename='inventory-items')

urlpatterns = router.urls + inventory_router.urls