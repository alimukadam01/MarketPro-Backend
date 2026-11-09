from django.contrib import admin

from core.admin_masking import MaskedModelAdmin

from .models import (
    City, Category, Customer, Location, 
    Product, Supplier, Unit, Business,
    Expense, ProductVariantType, 
    ProductVariant, Employee, EmployeeAccess, BusinessConfig,
    ProductField, ProductFieldLink,
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


@admin.register(ProductField)
class ProductFieldAdmin(MaskedModelAdmin):
    """Where a business's extra fields are made (plans/product-extra-fields.md):
    there is no screen for it. The shop then ticks them on its products.

    A field some product asks for cannot be deleted - Django refuses, as the
    link protects it. Untick Is active to retire it instead; old invoice lines
    keep showing their answers."""

    list_display = ('label', 'business', 'key', 'field_type', 'is_active')
    list_filter = ('is_active', 'field_type', 'business')
    search_fields = ('label', 'key', 'business__name')
    fields = ('business', 'label', 'key', 'field_type', 'is_active')


@admin.register(ProductFieldLink)
class ProductFieldLinkAdmin(MaskedModelAdmin):
    """Read-only, for support: the shop sets these on the product form."""

    list_display = ('product', 'field', 'applies_to', 'required')
    list_filter = ('applies_to', 'required')
    search_fields = ('product__name', 'field__label')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

