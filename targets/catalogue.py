"""
What can be measured, and which manager method measures it.

This module holds no field path, no Q object and no aggregate. Each entry
carries a reference to a manager method on the app that owns the model, because
the app that owns a model owns every number derived from it. That keeps a single
definition of "sales revenue" in the sales app, where a rename or a status
change updates it, instead of a second copy here that would drift silently.

It lives in Python rather than the database because it stores callables.
Keeping them as strings in a table and resolving them at runtime would be an
injection surface and would buy nothing: the catalogue is system-defined and
users cannot extend it.
"""
from dataclasses import dataclass
from typing import Callable, Tuple


@dataclass(frozen=True)
class DataPointSpec:
    """
    One measurable quantity.

    resolver is called as resolver(business_id, date_from, date_to, **filters)
    and returns a single number. Every allowed filter and subject maps to a
    keyword argument that resolver accepts; see targets.utils.resolver_kwargs.
    """
    code: str
    label: str
    unit: str                       # 'amount' | 'count'
    resolver: Callable
    allowed_filters: Tuple[str, ...]
    allowed_subjects: Tuple[str, ...]
    date_basis_label: str
    returns_treatment: str


def _catalogue():
    """
    Built lazily so this module can be imported without the app registry being
    ready. Managers are looked up at call time, not at import time.
    """
    from root.models import Expense
    from sales.models import (
        PurchaseInvoice, PurchaseInvoiceItem, SalesInvoice, SalesInvoiceItem,
    )
    from accounts.models import Transaction

    specs = (
        DataPointSpec(
            code='sales_revenue',
            label='Sales revenue',
            unit='amount',
            resolver=SalesInvoice.objects.revenue,
            allowed_filters=('customer', 'entered_by', 'invoice_status'),
            allowed_subjects=('customer', 'employee'),
            date_basis_label='Invoice date',
            returns_treatment='Cancelled invoices are always excluded. Drafts '
                              'are counted, as they are on the Sales page.',
        ),
        DataPointSpec(
            code='sales_invoice_count',
            label='Sales invoices raised',
            unit='count',
            resolver=SalesInvoice.objects.invoice_count,
            allowed_filters=('customer', 'entered_by', 'invoice_status'),
            allowed_subjects=('customer', 'employee'),
            date_basis_label='Invoice date',
            returns_treatment='Cancelled invoices are always excluded.',
        ),
        DataPointSpec(
            code='items_sold',
            label='Items sold (net of returns)',
            unit='count',
            resolver=SalesInvoiceItem.objects.net_items_sold,
            allowed_filters=('customer', 'product', 'product_variant',
                             'entered_by'),
            allowed_subjects=('customer', 'product', 'product_variant',
                              'employee'),
            date_basis_label='Invoice date',
            returns_treatment='Returned quantities are always subtracted. '
                              'Cancelled invoices are always excluded.',
        ),
        DataPointSpec(
            code='purchase_value',
            label='Purchase value',
            unit='amount',
            resolver=PurchaseInvoice.objects.purchase_value,
            allowed_filters=('supplier', 'entered_by', 'invoice_status'),
            allowed_subjects=('supplier', 'employee'),
            date_basis_label='Invoice date',
            returns_treatment='Cancelled invoices are always excluded. The '
                              'figure is gross: purchase invoices cannot '
                              'record a discount.',
        ),
        DataPointSpec(
            code='purchase_invoice_count',
            label='Purchase invoices raised',
            unit='count',
            resolver=PurchaseInvoice.objects.purchase_invoice_count,
            allowed_filters=('supplier', 'entered_by', 'invoice_status'),
            allowed_subjects=('supplier', 'employee'),
            date_basis_label='Invoice date',
            returns_treatment='Cancelled invoices are always excluded.',
        ),
        DataPointSpec(
            code='units_purchased',
            label='Units purchased',
            unit='count',
            resolver=PurchaseInvoiceItem.objects.units_purchased,
            allowed_filters=('supplier', 'product', 'product_variant'),
            allowed_subjects=('supplier', 'product', 'product_variant'),
            date_basis_label='Invoice date',
            returns_treatment='Gross. Purchase returns are not recorded '
                              'anywhere in the system, so nothing can be '
                              'subtracted.',
        ),
        DataPointSpec(
            code='cash_received',
            label='Cash received (net of refunds)',
            unit='amount',
            resolver=Transaction.objects.cash_received,
            allowed_filters=('payment_method',),
            allowed_subjects=('customer',),
            date_basis_label='Date the money moved',
            returns_treatment='Refunds paid back to customers are always '
                              'subtracted. Only cleared money counts, so a '
                              'pending or bounced cheque contributes nothing.',
        ),
        DataPointSpec(
            code='cash_paid',
            label='Cash paid (net of refunds received)',
            unit='amount',
            resolver=Transaction.objects.cash_paid,
            allowed_filters=('payment_method',),
            allowed_subjects=('supplier',),
            date_basis_label='Date the money moved',
            returns_treatment='Refunds received from suppliers are always '
                              'subtracted. Only cleared money counts.',
        ),
        DataPointSpec(
            code='expenses',
            label='Expenses',
            unit='amount',
            resolver=Expense.objects.expense_amount_in_range,
            allowed_filters=('expense_category',),
            allowed_subjects=(),
            date_basis_label='Date recorded',
            returns_treatment='None applicable. An expense carries only the '
                              'date it was entered, so one recorded late falls '
                              'in the month it was typed.',
        ),
    )

    return {spec.code: spec for spec in specs}


_CACHE = {}


def catalogue():
    """The nine specs, keyed by code. Built once per process."""
    if not _CACHE:
        _CACHE.update(_catalogue())
    return _CACHE


def get_spec(code):
    return catalogue().get(code)


def data_point_choices():
    return [(code, spec.label) for code, spec in catalogue().items()]


def allowed_dimensions(code):
    spec = get_spec(code)
    return spec.allowed_filters if spec else ()


def allowed_subject_types(code):
    spec = get_spec(code)
    return spec.allowed_subjects if spec else ()


### Dimensions -------------------------------------------------------------
#
# Each dimension is described once: whether it names an entity or picks from a
# fixed list, and how to confirm a supplied value belongs to this business.
# 'kwarg' is the keyword argument the resolvers accept for it.

ENTITY = 'entity'
CHOICE = 'choice'


def _entity_dimensions():
    from root.models import Customer, Product, ProductVariant, Supplier

    return {
        'customer': {
            'kind': ENTITY, 'kwarg': 'customer_id', 'label': 'Customer',
            'model': Customer, 'tenant_path': 'business_id',
            'name_field': 'name',
        },
        'supplier': {
            'kind': ENTITY, 'kwarg': 'supplier_id', 'label': 'Supplier',
            'model': Supplier, 'tenant_path': 'business_id',
            'name_field': 'name',
        },
        'product': {
            'kind': ENTITY, 'kwarg': 'product_id', 'label': 'Product',
            'model': Product, 'tenant_path': 'business_id',
            'name_field': 'name',
        },
        'product_variant': {
            'kind': ENTITY, 'kwarg': 'product_variant_id',
            'label': 'Product Variant', 'model': ProductVariant,
            # ProductVariant has no business FK of its own; it is reached
            # through the product it varies.
            'tenant_path': 'base__business_id', 'name_field': 'sku',
        },
        'entered_by': {
            'kind': ENTITY, 'kwarg': 'created_by_id', 'label': 'Entered By',
            'model': None,          # handled separately, see resolve_entity_label
            'tenant_path': None, 'name_field': 'email',
        },
    }


def _choice_dimensions():
    from root.models import Expense
    from sales.models import SalesInvoice
    from accounts.models import Transaction

    return {
        'invoice_status': {
            'kind': CHOICE, 'kwarg': 'status', 'label': 'Invoice Status',
            # Sales statuses by default. A purchase data point overrides this
            # in catalogue_payload() below, because the two sets differ and 'C'
            # means opposite things in each.
            'choices': [
                (code, label)
                for code, label in SalesInvoice.SALES_INVOICE_STATUS_CHOICES
                if code not in SalesInvoice.CANCELLED_STATUSES
            ],
        },
        'payment_method': {
            'kind': CHOICE, 'kwarg': 'payment_method',
            'label': 'Payment Method',
            'choices': list(Transaction.PAYMENT_METHOD_CHOICES),
        },
        'expense_category': {
            'kind': CHOICE, 'kwarg': 'category', 'label': 'Category',
            'choices': list(Expense.CATEGORY_CHOICES),
        },
    }


_DIM_CACHE = {}


def dimensions():
    if not _DIM_CACHE:
        _DIM_CACHE.update(_entity_dimensions())
        _DIM_CACHE.update(_choice_dimensions())
    return _DIM_CACHE


def get_dimension(name):
    return dimensions().get(name)


def dimension_kwarg(name):
    dimension = get_dimension(name)
    return dimension['kwarg'] if dimension else None


def subject_kwarg(subject_type):
    """
    A subject narrows the same way a filter does, so it reuses the dimension
    registry. 'employee' is the exception: it is stored as a user id because
    every ORM path in the resolvers is created_by_id.
    """
    if subject_type == 'employee':
        return 'created_by_id'
    return dimension_kwarg(subject_type)


def _purchase_status_choices():
    from sales.models import PurchaseInvoice

    return [
        (code, label)
        for code, label in PurchaseInvoice.PURCHASE_INVOICE_STATUS_CHOICES
        if code not in PurchaseInvoice.CANCELLED_STATUSES
    ]


PURCHASE_DATA_POINTS = (
    'purchase_value', 'purchase_invoice_count', 'units_purchased',
)


def resolve_entity_label(dimension_name, value_id, business):
    """
    Confirms an entity belongs to this business and returns its display name.

    Returns None when it does not, which callers treat as a validation error.
    The name is stored on the filter row so a card survives the entity being
    deleted later.
    """
    dimension = get_dimension(dimension_name)
    if not dimension or dimension['kind'] != ENTITY:
        return None

    if dimension_name == 'entered_by':
        # Either the owner or somebody on staff. Employee rows carry a user FK,
        # and it is the user id that the resolvers filter on.
        if business.owner_id == value_id:
            return business.owner.email
        employee = business.emp_records.filter(user_id=value_id).first()
        return employee.user.email if employee else None

    row = (dimension['model'].objects
           .filter(**{dimension['tenant_path']: business.id, 'id': value_id})
           .first())
    if not row:
        return None
    return str(getattr(row, dimension['name_field'], '') or row.id)


def resolve_choice_label(dimension_name, value_text, data_point=None):
    """
    Confirms a choice value is one this dimension offers and returns its label.
    Returns None otherwise.
    """
    dimension = get_dimension(dimension_name)
    if not dimension or dimension['kind'] != CHOICE:
        return None

    choices = dimension['choices']
    if dimension_name == 'invoice_status' and data_point in PURCHASE_DATA_POINTS:
        choices = _purchase_status_choices()

    for code, label in choices:
        if code == value_text:
            return label
    return None


def catalogue_payload():
    """
    What GET /targets/catalogue/ returns.

    Entity options are deliberately left out. A shop can have thousands of
    customers, so the form fetches them from the existing list endpoints the
    same way every other form in the app does.
    """
    payload = []

    for code, spec in catalogue().items():
        filters = []
        for name in spec.allowed_filters:
            dimension = get_dimension(name)
            if not dimension:
                continue
            entry = {
                'code': name,
                'label': dimension['label'],
                'kind': dimension['kind'],
            }
            if dimension['kind'] == CHOICE:
                choices = dimension['choices']
                if name == 'invoice_status' and code in PURCHASE_DATA_POINTS:
                    choices = _purchase_status_choices()
                entry['options'] = [
                    {'value': value, 'label': label} for value, label in choices
                ]
            filters.append(entry)

        subjects = []
        for name in spec.allowed_subjects:
            if name == 'employee':
                subjects.append({'code': 'employee', 'label': 'Employee'})
                continue
            dimension = get_dimension(name)
            if dimension:
                subjects.append({'code': name, 'label': dimension['label']})

        payload.append({
            'code': code,
            'label': spec.label,
            'unit': spec.unit,
            'date_basis_label': spec.date_basis_label,
            'returns_treatment': spec.returns_treatment,
            'filters': filters,
            'subject_types': subjects,
        })

    return payload
