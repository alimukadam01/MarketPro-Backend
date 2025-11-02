from rest_framework_nested.routers import NestedDefaultRouter, DefaultRouter
from .views import (
    PurchaseInvoiceItemViewSet, 
    PurchaseInvoiceViewSet, 
    SalesInvoiceItemViewSet, 
    SalesInvoiceViewSet,
    ReturnedItemsViewSet
)

router = DefaultRouter()
router.register('purchase-invoices', PurchaseInvoiceViewSet, basename='purchase_invoices')
router.register('sales-invoices', SalesInvoiceViewSet, basename='sales_invoices')
router.register('returned-items', ReturnedItemsViewSet, basename='returned_items')

purchase_invoice_router = NestedDefaultRouter(router, 'purchase-invoices', lookup='purchase_invoice')
purchase_invoice_router.register('items', PurchaseInvoiceItemViewSet, basename='purchase_invoice_items')

sales_invoice_router = NestedDefaultRouter(router, 'sales-invoices', lookup='sales_invoice')
sales_invoice_router.register('items', SalesInvoiceItemViewSet, basename='sales_invoice_items')

urlpatterns = router.urls + purchase_invoice_router.urls + sales_invoice_router.urls