from django.contrib import admin

from core.admin_masking import MaskedModelAdmin

from .models import (
    City, Category, Customer, Location, 
    Product, Supplier, Unit, Business,
    Expense, ProductVariantType, 
    ProductVariant, Employee, EmployeeAccess, BusinessConfig
)

# Registered through MaskedModelAdmin so currency amounts render as asterisks.
# Every model goes through it, not only the two that hold an amount: the mixin
# is a no-op for models in neither registry, and blanket registration is what
# masks the FK dropdowns and columns pointing at amount-bearing models.
for model in (
    City, Category, Unit, Business, BusinessConfig, Employee, EmployeeAccess,
    Customer, Location, Product, ProductVariant, ProductVariantType, Supplier,
    Expense,
):
    admin.site.register(model, MaskedModelAdmin)

