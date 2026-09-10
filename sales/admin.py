from django.contrib import admin

from core.admin_masking import MaskedModelAdmin

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


# Registered through MaskedModelAdmin so currency amounts render as asterisks.
# SalesInvoice and PurchaseInvoice also carry their total in __str__, so their
# changelists and every FK dropdown targeting them are masked too.
for model in (
    SalesInvoice, SalesInvoiceItem, PurchaseInvoice, PurchaseInvoiceItem,
    PurchaseInvoiceItemRestock, SalesInvoiceItemDeduction, SalesReservation,
    ReturnedItem, PurchaseQuotation, PurchaseQuotationItem,
):
    admin.site.register(model, MaskedModelAdmin)