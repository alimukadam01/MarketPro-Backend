from django.contrib import admin

from core.admin_masking import MaskedModelAdmin

from .models import Project, ProjectPurchaseInvoice, ProjectSalesInvoice, ProjectPurchaseQuotation

# Registered through MaskedModelAdmin so currency amounts render as asterisks.
# These join models hold no amounts of their own, but their add forms list every
# sales and purchase invoice in a dropdown -- each labelled with its total.
for model in (
    Project, ProjectPurchaseInvoice, ProjectSalesInvoice,
    ProjectPurchaseQuotation,
):
    admin.site.register(model, MaskedModelAdmin)