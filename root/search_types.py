"""What the root app makes searchable: products and their variants,
locations, customers, suppliers, expenses and employees.

Here rather than in root/search.py because `root.search` is the search
package itself; RootConfig.ready() imports this module.
"""
from root.search import Hit, SearchType, admin_only, link, register, status
from root.search.labels import joined, person_name, product_label

from .models import Employee, Expense, Location, Person, Product, ProductVariant

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

def _person_roles(person):
    roles = []
    for kind, label in (("customer", "Customer"), ("supplier", "Supplier")):
        # A missing reverse one-to-one raises an AttributeError subclass.
        role = getattr(person, kind, None)
        if role is not None:
            roles.append(label if role.is_active else f"{label} (inactive)")
    return ", ".join(roles)


# One result per person, whatever roles they hold (People, register R33).
register(SearchType(
    key="person",
    label="People",
    model=Person,
    module="people",
    text_fields=["name", "business_name", "email", "address", "city__name", "notes"],
    number_fields=["id"],
    phone_fields=["phone"],
    select_related=["city", "customer", "supplier"],
    order_by=["name", "id"],
    priority=150,
    to_hit=lambda person: Hit(
        id=person.id,
        title=person.name,
        subtitle=joined(_person_roles(person), getattr(person.city, "name", ""), person.phone),
        link=link("/people/view-person", person_id=person.id),
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
