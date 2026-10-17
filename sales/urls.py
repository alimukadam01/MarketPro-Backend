from rest_framework_nested.routers import NestedDefaultRouter, DefaultRouter
from .views import (
    PurchaseInvoiceImageViewSet,
    PurchaseInvoiceItemViewSet, 
    PurchaseInvoiceViewSet,
    PurchaseQuotationItemViewSet,
    PurchaseQuotationViewSet,
    PurchaseReceiptsViewSet,
    PurchasesKPIViewSet,
    ReturnedItemsKPIViewSet, 
    SalesInvoiceImageViewSet,
    SalesInvoiceItemViewSet, 
    SalesInvoiceViewSet,
    ReturnedItemsViewSet,
    SalesKPIViewSet,
    SalesReceiptsViewSet
)

router = DefaultRouter()
router.register('purchase-invoices', PurchaseInvoiceViewSet, basename='purchase_invoices')
router.register('sales-invoices', SalesInvoiceViewSet, basename='sales_invoices')
router.register('returned-items', ReturnedItemsViewSet, basename='returned_items')
router.register('purchase-quotations', PurchaseQuotationViewSet, basename='purchase_quotations')

router.register('sales-kpis', SalesKPIViewSet, basename='sales-kpis')
router.register('purchases-kpis', PurchasesKPIViewSet, basename='purchases-kpis')
router.register('returned-items-kpis', ReturnedItemsKPIViewSet, basename='returned-items-kpis')

purchase_invoice_router = NestedDefaultRouter(router, 'purchase-invoices', lookup='purchase_invoice')
purchase_invoice_router.register('items', PurchaseInvoiceItemViewSet, basename='purchase_invoice_items')
purchase_invoice_router.register('payments', PurchaseReceiptsViewSet, basename='payment_receipts')
purchase_invoice_router.register('images', PurchaseInvoiceImageViewSet, basename='purchase_invoice_images')

sales_invoice_router = NestedDefaultRouter(router, 'sales-invoices', lookup='sales_invoice')
sales_invoice_router.register('items', SalesInvoiceItemViewSet, basename='sales_invoice_items')
sales_invoice_router.register('payments', SalesReceiptsViewSet, basename='payment_reciepts')
sales_invoice_router.register('images', SalesInvoiceImageViewSet, basename='sales_invoice_images')

quotations_router = NestedDefaultRouter(router, 'purchase-quotations', lookup='purchase_quotation')
quotations_router.register('items', PurchaseQuotationItemViewSet, basename='purchase_quotation_items')

urlpatterns = router.urls + purchase_invoice_router.urls + sales_invoice_router.urls + quotations_router.urls