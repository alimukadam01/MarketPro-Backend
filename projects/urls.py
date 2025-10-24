from rest_framework_nested.routers import DefaultRouter, NestedDefaultRouter
from .views import ProjectPurchaseInvoiceViewSet, ProjectSalesInvoiceViewSet, ProjectViewSet

router = DefaultRouter()
router.register('projects', ProjectViewSet, basename='project')

projects_router = NestedDefaultRouter(router, 'projects', lookup='project')
projects_router.register('sales-invoices', ProjectSalesInvoiceViewSet, basename='project-sales-invoices')
projects_router.register('purchase-invoices', ProjectPurchaseInvoiceViewSet, basename='project-purchase-invoices')

urlpatterns = router.urls + projects_router.urls