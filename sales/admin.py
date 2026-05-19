from django.contrib import admin
from .models import (
    SalesInvoice, 
    SalesInvoiceItem, 
    PurchaseInvoice, 
    PurchaseInvoiceItem, 
    PurchaseInvoiceItemRestock, 
    SalesInvoiceItemDeduction, 
    SalesReservation,
    ReturnedItem,
    PurchaseQuotation,
    PurchaseQuotationItem
)


# Register your models here.

admin.site.register(SalesInvoice)
admin.site.register(SalesInvoiceItem)
admin.site.register(PurchaseInvoice)
admin.site.register(PurchaseInvoiceItem)
admin.site.register(PurchaseInvoiceItemRestock)
admin.site.register(SalesInvoiceItemDeduction)
admin.site.register(SalesReservation)
admin.site.register(ReturnedItem)
admin.site.register(PurchaseQuotation)
admin.site.register(PurchaseQuotationItem)