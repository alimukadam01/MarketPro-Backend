from django.db import models
from sales.models import PurchaseInvoice, PurchaseQuotation, SalesInvoice
from root.models import Business, Customer, Product
from django.conf import settings

# Create your models here.

class Project(models.Model):

    STATUS_CHOICES = [
        ("P", "Planned"),
        ("IP", "In Progress"),
        ("C", "Completed"),
        ("X", "Cancelled"),
    ]

    title = models.CharField(max_length=255)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name='projects')
    customer = models.ForeignKey(
        Customer, on_delete=models.SET_NULL, 
        related_name='projects', null=True, blank=True
    )
    status = models.CharField(max_length=256, choices=STATUS_CHOICES, default="P")
    description = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"{self.title} - {self.business.name} ({self.status})"


class ProjectSalesInvoice(models.Model):
    project = models.ForeignKey(Project, models.CASCADE, related_name='sales_invoices')
    sales_invoice = models.ForeignKey(SalesInvoice, models.CASCADE, related_name='projects')


class ProjectPurchaseInvoice(models.Model):
    project = models.ForeignKey(Project, models.CASCADE, related_name='purchase_invoices')
    purchase_invoice = models.ForeignKey(PurchaseInvoice, models.CASCADE, related_name='projects')


class ProjectPurchaseQuotation(models.Model):
    project = models.ForeignKey(Project, models.CASCADE, related_name='purchase_quotations')
    purchase_quotation = models.ForeignKey(PurchaseQuotation, models.CASCADE, related_name='projects')