from django.contrib import admin
from .models import Project, ProjectPurchaseInvoice, ProjectSalesInvoice, ProjectPurchaseQuotation

# Register your models here.

admin.site.register(Project)
admin.site.register(ProjectPurchaseInvoice)
admin.site.register(ProjectSalesInvoice)
admin.site.register(ProjectPurchaseQuotation)