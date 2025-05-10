from django.contrib import admin
from .models import SalesInvoice, SalesInvoiceItem, PurchaseInvoice, PurchaseInvoiceItem

# Register your models here.

admin.site.register(SalesInvoice)
admin.site.register(SalesInvoiceItem)
admin.site.register(PurchaseInvoice)
admin.site.register(PurchaseInvoiceItem)
