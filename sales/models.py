from django.db import models
from django.conf import settings
from root.models import Business, Customer, BaseItem, Supplier

# Create your models here.

class SalesInvoice(models.Model):

    SALES_INVOICE_STATUS_CHOICES = [
        ("D", "DRAFT"),
        ("S", "SENT"),
        ("P", "PAID"),
        ("C", "CANCELLED")
    ]

    invoice_number = models.CharField(max_length=256)
    business = models.ForeignKey(Business, models.CASCADE)
    customer = models.ForeignKey(Customer, models.CASCADE, related_name='sale_invoices')
    date_issued = models.DateTimeField(auto_now_add=True)
    date_due = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=256, choices=SALES_INVOICE_STATUS_CHOICES, default=SALES_INVOICE_STATUS_CHOICES[2])
    sub_total = models.FloatField(null=True, blank=True)
    tax = models.FloatField(null=True, blank=True)
    discount = models.IntegerField(null=True, blank=True)
    total = models.FloatField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, models.CASCADE, 'created_sales_invoices')
    created_at = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(null=True, blank=True)
    

class SalesInvoiceItem(BaseItem):
    sales_invoice = models.ForeignKey(SalesInvoice, models.CASCADE, related_name='invoice_items')
    unit_price = models.FloatField(null=True, blank=True)
    discount = models.IntegerField(null=True, blank=True)

    class Meta:                  
        unique_together = [('sales_invoice', 'product')] 


class PurchaseInvoice(models.Model):

    PURCHASE_INVOICE_STATUS_CHOICES = [
        ("D", "DRAFT"),
        ("R", "RECEIVED"),
        ("P", "PAID"),
        ("C", "CANCELLED")
    ]

    invoice_number = models.CharField(max_length=256, null=True, blank=True)
    business = models.ForeignKey(Business, models.CASCADE, related_name='purchase_invoices')
    supplier = models.ForeignKey(Supplier, models.DO_NOTHING, related_name='purchase_invoices')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    date_due = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=256, choices=PURCHASE_INVOICE_STATUS_CHOICES, default=PURCHASE_INVOICE_STATUS_CHOICES[1])
    sub_total = models.FloatField()
    tax = models.FloatField(null=True, blank=True)
    total = models.FloatField()  
    delivery = models.DateField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, models.DO_NOTHING, related_name='created_purchase_invoices')  
    notes = models.TextField(null=True, blank=True)


class PurchaseInvoiceItem(BaseItem):
    purchase_invoice = models.ForeignKey(PurchaseInvoice, models.CASCADE, related_name='invoice_items')
    unit_cost = models.FloatField()

    class Meta:
        unique_together = [('purchase_invoice', 'product')]

