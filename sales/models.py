from typing import Dict
from django.db import models
from django.core.exceptions import ValidationError
from django.conf import settings
from root.utils import generateTransactionId
from root.models import Business, Customer, BaseItem, Supplier, Location


# Create your models here.

class BaseRestock(models.Model):

    quantity = models.PositiveIntegerField()
    received_at = models.DateField(null=True, blank=True)
    notes = models.TextField(null=True, blank=True)  # Optional link to external txn

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        ordering = ['-created_at']


class SalesInvoice(models.Model):

    SALES_INVOICE_STATUS_CHOICES = [
        ("D", "DRAFT"),
        ("S", "SENT"),
        ("O", "OVERDUE"),
        ("X", "CANCELLED"),
        ("C", "COMPLETED"),
        ("PC", "PARTIALLY_COMPLETED")
    ]

    PAYMENT_STATUS_CHOICES = [
        ("P", "PAID"),
        ("PP", "PARTIALLY_PAID"),
        ("PEN", "PENDING"),
        ("RF", "REFUNDED"),
        ("C", "CANCELLED"),
    ]

    invoice_number = models.CharField(max_length=256)
    business = models.ForeignKey(Business, models.CASCADE)
    customer = models.ForeignKey(Customer, models.CASCADE, related_name='sale_invoices')
    date_issued = models.DateTimeField(auto_now_add=True)
    date_due = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=256, choices=SALES_INVOICE_STATUS_CHOICES, default=SALES_INVOICE_STATUS_CHOICES[2])
    payment_status = models.CharField(max_length=256, choices=PAYMENT_STATUS_CHOICES, default=PAYMENT_STATUS_CHOICES[2])
    sub_total = models.FloatField(null=True, blank=True)
    tax = models.JSONField(
        null=True,
        blank=True,
        default=dict,
        help_text='e.g., {"value": 10.0, "type": "percentage" or "amount"}'
    )
    discount = models.JSONField(
        null=True,
        blank=True,
        default=dict,
        help_text='e.g., {"value": 10.0, "type": "percentage" or "amount"}'
    )
    total = models.FloatField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, models.CASCADE, 'created_sales_invoices')
    created_at = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(null=True, blank=True)
    is_deducted = models.BooleanField(default=False)
    is_partially_deducted = models.BooleanField(default=False)

    def update_deduction_flags(self):
        items = self.invoice_items.all()
        deducted = 0

        for obj in items:
            if obj.is_partially_deducted:
                self.is_partially_deducted = True
                self.is_deducted = False
                self.status = 'PC'
                return
            
            if obj.is_deducted:
                deducted += 1

        if deducted == len(items):
            self.is_partially_deducted = False
            self.is_deducted = True
            self.status = 'C'

    def __str__(self):
        return f"{self.id}-{self.invoice_number}-{self.total}-{self.status}"

    def adjust_totals(self):
        items = self.invoice_items.all()
        if not items: return

        subtotal = sum(item.quantity * item.unit_price for item in items)
        
        ### Calculate total discount on the order.
        discount = 0
        if self.discount:
            if self.discount["type"] == "percentage":
                discount += self.discount["value"]/100 * subtotal

            elif self.discount["type"] == "amount":
                discount += self.discount["value"]

            else:
                raise Exception("incorrect value")
        
        else:
            for item in items:
                if not item.discount:
                    continue

                if item.discount["type"] == "percentage":
                    discount += item.unit_price * (item.discount["value"]/100)

                elif item.tax["type"] == "amount":
                    discount += item.discount["value"]

                else:
                    raise Exception("incorrect value")
                    
        ### Calculate tax on the Invoice
        tax = 0            
        if self.tax:
            if self.tax['type'] == "percentage":
                tax = subtotal * (self.tax['value']/100)

            elif self.tax['type'] == "amount":
                tax += self.tax['value']

            else:
                raise Exception("incorrect value")
            
        self.sub_total = subtotal
        self.total = subtotal + tax - discount
        self.save(update_fields=["sub_total", "total"])

    def is_fulfilled(self):
        for item in self.invoice_items.all():
            if not item.is_restocked:
                self.is_restocked = False
                return
            
        self.is_restocked = True
        self.is_partially_restocked = False
        self.status = 'C'

    def map_to_products(self) -> Dict[int, 'SalesInvoiceItem']:
        return {item.product_id: item for item in self.invoice_items.all()}
    

class SalesInvoiceItem(BaseItem):
    sales_invoice = models.ForeignKey(SalesInvoice, models.CASCADE, related_name='invoice_items')
    unit_price = models.FloatField(null=True, blank=True)
    discount = models.JSONField(
        null=True,
        blank=True,
        default=dict,
        help_text='e.g., {"value": 10.0, "type": "percentage" or "amount"}'
    )
    quantity_received = models.IntegerField(null=True, blank=True)
    is_deducted = models.BooleanField(default=False)
    is_partially_deducted = models.BooleanField(default=False)
    
    def compute_restock_delta(self) -> int:
        """
        Sum up all past restocks to find out how many units remain to add.
        """
        agg = self.restocks.aggregate(total=models.Sum('quantity'))
        prev = agg.get('total') or 0
        return getattr(self, 'quantity_received') - prev

    class Meta:                  
        unique_together = [('sales_invoice', 'product')]

    def update_restock_flags(self):
        """
        Exactly one of is_restocked / is_partially_restocked should be True.
        """

        full = (self.quantity_received >= self.quantity)
        self.is_deducted = full
        self.is_partially_deducted = not full
        self.save(update_fields=['is_deducted', 'is_partially_deducted'])


class PurchaseInvoice(models.Model):

    PURCHASE_INVOICE_STATUS_CHOICES = [
        ("D", "DRAFT"),
        ("R", "RECEIVED"),
        ("PR", "PARTIALLY_RECEIVED"),
        ("C", "CANCELLED"),
        ("O", "OVERDUE"),
    ]

    PAYMENT_STATUS_CHOICES = [
        ("P", "PAID"),
        ("PP", "PARTIALLY_PAID"),
        ("PEN", "PENDING"),
        ("RF", "REFUNDED"),
        ("C", "CANCELLED")
    ]

    invoice_number = models.CharField(max_length=256, null=True, blank=True)
    business = models.ForeignKey(Business, models.CASCADE, related_name='purchase_invoices')
    supplier = models.ForeignKey(Supplier, models.DO_NOTHING, related_name='purchase_invoices')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    date_due = models.DateField(null=True, blank=True)
    tax = models.JSONField(
        null=True,
        blank=True,
        default=dict,
        help_text='e.g., {"value": 10.0, "type": "percentage" or "amount"}'
    )
    status = models.CharField(max_length=256, choices=PURCHASE_INVOICE_STATUS_CHOICES, default=PURCHASE_INVOICE_STATUS_CHOICES[1])
    payment_status = models.CharField(max_length=256, choices=PAYMENT_STATUS_CHOICES, default=PAYMENT_STATUS_CHOICES[2])
    sub_total = models.FloatField(null=True, blank=True)
    total = models.FloatField(null=True, blank=True)
    amount_paid = models.FloatField(null=True, blank=True)
    goods_received = models.IntegerField(null=True, blank=True)
    delivery = models.DateField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, models.DO_NOTHING, related_name='created_purchase_invoices')  
    notes = models.TextField(null=True, blank=True)
    is_restocked = models.BooleanField(default=False)
    is_partially_restocked = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.id}-{self.invoice_number}-{self.total}-{self.status}"

    def adjust_totals(self):
        items = self.invoice_items.all()  # Assuming related_name='items' in SalesInvoiceItem
        subtotal = sum(item.quantity * item.unit_cost for item in items)
                    
        ### Calculate tax on the Invoice
        tax = 0            
        if self.tax:
            if self.tax["type"] == "percentage":
                tax = subtotal * (self.tax['value']/100)

            elif self.tax['type'] == "amount":
                tax += self.tax['value']

            else:
                raise Exception("incorrect value")
            
        self.sub_total = subtotal
        self.total = subtotal + tax
        self.save(update_fields=["sub_total", "total"])

    def is_fulfilled(self): 
        for item in self.invoice_items.all():
            if not item.is_restocked:
                self.is_restocked = False
                self.is_partially_restocked = True
                return
            
        self.is_restocked = True
        self.is_partially_restocked = False
        self.status = 'R'

    def map_to_products(self) -> Dict[int, 'PurchaseInvoiceItem']:
        return {item.product_id: item for item in self.invoice_items.all()}
    
    def update_restock_flags(self):

        items = self.invoice_items.all()
        restocked = 0

        for obj in items:
            if obj.is_partially_restocked:
                self.is_partially_restocked = True

            if obj.is_restocked:
                restocked += 1

        if restocked == len(items):
            self.is_partially_restocked = False
            self.is_restocked = True
            self.status = 'R'
            
                
class PurchaseInvoiceItem(BaseItem):
    purchase_invoice = models.ForeignKey(PurchaseInvoice, models.CASCADE, related_name='invoice_items')
    unit_cost = models.FloatField()
    quantity_received = models.IntegerField(null=True, blank=True)
    is_restocked = models.BooleanField(default=False)
    is_partially_restocked = models.BooleanField(default=False)

    def clean(self):
        super().clean()
        if self.is_restocked and self.is_partially_restocked:
            raise ValidationError("Either 'is_restocked' or 'is_partially_restocked' must be True, but not both.")
    
    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def morph(self):

        try:
            location = self.business.locations.get(is_default=True)
        except Location.DoesNotExist:
            location = None

        return {
            "business_id": self.business.id,
            "inventory_id": self.business.inventory_glance.id,
            "location_id": location.id if location else None,       
            "product_id": self.product.id,
            "quantity": self.quantity,
            "track_code": self.track_code,
            "notes": self.notes,
            "last_transaction": generateTransactionId(self),
            "unit_cost": self.unit_cost,
            "quantity_received": self.quantity_received
        }
    
    def compute_restock_delta(self) -> int:
        """
        Sum up all past restocks to find out how many units remain to add.
        """
        agg = self.restocks.aggregate(total=models.Sum('quantity'))
        prev = agg.get('total') or 0
        return getattr(self, 'quantity_received') - prev
    
    def update_restock_flags(self):
        """
        Exactly one of is_restocked / is_partially_restocked should be True.
        """

        full = (self.quantity_received >= self.quantity)
        self.is_restocked = full
        self.is_partially_restocked = not full
        self.save(update_fields=['is_restocked', 'is_partially_restocked'])


    class Meta:
        unique_together = [('purchase_invoice', 'product')]


class PurchaseInvoiceItemRestock(BaseRestock):
    purchase_invoice = models.ForeignKey(PurchaseInvoice, models.CASCADE, related_name='restocks')
    purchase_invoice_item = models.ForeignKey(PurchaseInvoiceItem, models.CASCADE, related_name='restocks')

    def __str__(self):
        return f"{self.purchase_invoice.id}: {self.purchase_invoice_item.product.name} x {self.quantity}"
    

class SalesInvoiceItemDeduction(BaseRestock):
    sales_invoice = models.ForeignKey(SalesInvoice, models.CASCADE, related_name='restocks')
    sales_invoice_item = models.ForeignKey(SalesInvoiceItem, models.CASCADE, related_name='restocks')

    def __str__(self):
        return f"{self.sales_invoice.id}: {self.sales_invoice_item.product.name} x {self.quantity}"


class SalesReservation(BaseRestock):
    sales_invoice = models.ForeignKey(SalesInvoice, models.CASCADE, related_name='deductions')
    sales_invoice_item = models.ForeignKey(SalesInvoiceItem, models.CASCADE, related_name='deductions')

    def __str__(self):
        return f"{self.sales_invoice.id}: {self.sales_invoice_item.product.name} x {self.quantity}"
    

class PurchaseQuotation(models.Model):

    PQ_STATUSES = [
        ("D", "Draft"),
        ("S", "Sent"),
        ("A", "Accepted"),
        ("R", "Rejected"),
        ("X", "Cancelled"),
    ]

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name='purchase_quotations')
    quotation_no  = models.CharField(max_length=256, null=True, blank=True)
    expected_response_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    status = models.CharField(max_length=256, choices=PQ_STATUSES, default="D")
    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)
    suppliers = models.ManyToManyField(Supplier, through='PurchaseQuotationSupplier')
    notes = models.TextField(null=True, blank=True)


class PurchaseQuotationItem(BaseItem):
    purchase_quotation = models.ForeignKey(PurchaseQuotation, on_delete=models.CASCADE, related_name='items')
    unit_price = models.FloatField(null=True, blank=True)


class PurchaseQuotationSupplier(models.Model):
    purchase_quotation = models.ForeignKey(PurchaseQuotation, on_delete=models.CASCADE)
    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE)
    is_confirmed = models.BooleanField(default=False)

    def save(self, *args, **kwargs):
        
        if self.is_confirmed:
            PurchaseQuotationSupplier.objects.filter(
                purchase_quotation=self.purchase_quotation, 
                is_confirmed=True
            ).exclude(pk=self.pk).update(is_confirmed=False)

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.supplier.name}-{self.purchase_quotation.quotation_no if self.purchase_quotation.quotation_no else self.purchase_quotation.id}"

    #SalesQuotation*
    #Payment*
    #Inter Account Transfer
    #Bank Reconciliations
    #Witholding Tax 
    #Debit Notes
    #Inventory Write Offs
    #Production Orders
    #Employees
    #Employee Payslips
    #Master Account Functionality
    #Folders
    #Financial Reports
    