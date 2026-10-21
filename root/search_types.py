"""What the root app makes searchable: products and their variants,
locations, customers, suppliers, expenses and employees.

Here rather than in root/search.py because `root.search` is the search
package itself; RootConfig.ready() imports this module.
"""
from root.search import Hit, SearchType, admin_only, link, register, status
from root.search.labels import joined, person_name, product_label

from .models import Customer, Employee, Expense, Location, Product, ProductVariant, Supplier

INACTIVE = status("OFF", "Inactive", "neutral")

# ── Catalogue ────────────────────────────────────────────────────────────────

register(SearchType(
    key="product",
    label="Products",
    model=Product,
    module="products",
    text_fields=["name", "code", "desc", "unit__name"],
    number_fields=["id"],
    select_related=["unit"],
    order_by=["name", "id"],
    priority=120,
    to_hit=lambda product: Hit(
        id=product.id,
        title=product.name,
        subtitle=joined(getattr(product.unit, "name", ""), product.code),
        status=None if product.is_active else INACTIVE,
        link=link("/products/update-product", product_id=product.id),
    ),
))

register(SearchType(
    key="product_variant",
    label="Product variants",
    model=ProductVariant,
    module="products",
    business_field="base__business",
    text_fields=["name", "sku", "base__name", "base__code"],
    number_fields=["id"],
    select_related=["base"],
    order_by=["base__name", "name", "id"],
    priority=130,
    to_hit=lambda variant: Hit(
        id=variant.id,
        title=product_label(variant),
        subtitle=joined("SKU", variant.sku, sep=" ") if variant.sku else "",
        status=None if variant.is_active else INACTIVE,
        link=link("/products/update-product", product_id=variant.base_id),
    ),
))

register(SearchType(
    key="location",
    label="Locations",
    model=Location,
    module="locations",
    text_fields=["name", "address"],
    number_fields=["id"],
    order_by=["name", "id"],
    priority=140,
    to_hit=lambda location: Hit(
        id=location.id,
        title=location.name,
        subtitle=location.address or "",
        status=status("DEF", "Default", "neutral") if location.is_default else None,
        link=link("/locations/update-location", location_id=location.id),
    ),
))

# ── People ───────────────────────────────────────────────────────────────────

register(SearchType(
    key="customer",
    label="Customers",
    model=Customer,
    module="customers",
    text_fields=["name", "email", "address", "city__name", "notes"],
    number_fields=["id"],
    phone_fields=["phone"],
    select_related=["city"],
    order_by=["name", "id"],
    priority=150,
    to_hit=lambda customer: Hit(
        id=customer.id,
        title=customer.name,
        subtitle=joined(getattr(customer.city, "name", ""), customer.phone),
        link=link("/customers/view-customer", customer_id=customer.id),
    ),
))

register(SearchType(
    key="supplier",
    label="Suppliers",
    model=Supplier,
    module="suppliers",
    text_fields=["name", "business_name", "email", "notes"],
    number_fields=["id"],
    phone_fields=["phone"],
    order_by=["name", "id"],
    priority=160,
    to_hit=lambda supplier: Hit(
        id=supplier.id,
        title=supplier.name,
        subtitle=joined(supplier.business_name, supplier.phone),
        link=link("/suppliers/view-supplier", supplier_id=supplier.id),
    ),
))

register(SearchType(
    key="employee",
    label="Employees",
    model=Employee,
    access=admin_only,
    text_fields=["user__first_name", "user__last_name", "user__email"],
    phone_fields=["user__phone"],
    select_related=["user"],
    order_by=["user__first_name", "user__last_name", "id"],
    priority=170,
    to_hit=lambda employee: Hit(
        id=employee.id,
        title=person_name(employee.user),
        subtitle=employee.user.email,
        link=link("/employees"),
    ),
))

# ── Money ────────────────────────────────────────────────────────────────────

register(SearchType(
    key="expense",
    label="Expenses",
    model=Expense,
    module="expenses",
    text_fields=["name", "desc", "sales_invoice__invoice_number"],
    number_fields=["id", "amount"],
    choice_fields={"category": None},
    select_related=["transaction_record"],
    order_by=["-created_at", "-id"],
    priority=200,
    to_hit=lambda expense: Hit(
        id=expense.id,
        title=expense.name,
        subtitle=expense.get_category_display(),
        amount=expense.amount,
        date=expense.get_expense_date(),
        link=link("/expenses/update-expense", expense_id=expense.id),
    ),
))
