"""
Admin-side masking for currency amounts.

Every rupee figure that ``/admin/`` would otherwise render is replaced with a
fixed run of asterisks, while the fields stay editable: leave a masked box
untouched and the stored value is kept, type a number and it replaces it.

The value is not styled to look hidden -- it never reaches the browser at all.
``MaskedAmountInput`` hands the widget ``None``, and Django's ``input.html``
only writes a ``value`` attribute when the value is not ``None``, so there is
nothing in the response body for developer tools to find.

This module deliberately imports no project models. Both registries are keyed by
``"app_label.ModelName"`` strings and resolved at call time from
``obj._meta.label``, so every app's ``admin.py`` can import it with no risk of an
import cycle.

Masking happens at the four points where a value would become text -- later than
the queryset, earlier than the HTML:

    form input           MaskedAmountInput.get_context
    amount column        get_list_display
    FK <select> / column formfield_for_foreignkey / get_list_display
    subtitle, breadcrumb render_change_form

Three rules for anyone editing this file:

1. Never mask at the queryset layer. Blanking the field on the instance would
   also blank what ``ModelAdmin.get_object()`` returns, and the "blank keeps the
   stored value" restore below reads exactly that -- a blank submit would then
   write ``"********"`` into a FloatField.

2. Never censor a rendered string. ``str(obj).replace(str(amount), MASK)`` fails
   *open* whenever the repr differs from the stored value (``45000`` vs
   ``45000.0``, ``None``, localisation), and over-masks when the amount happens
   to appear elsewhere in the label. ``MASKED_LABELS`` rebuilds each label from
   the non-secret attributes instead and never reads the amount, so its worst
   failure mode is a stale label rather than a leak.

3. Never give a masked column an ``admin_order_field``, and never name a masked
   field in ``ordering``, ``sortable_by`` or ``date_hierarchy``. A sortable
   masked column is a binary-search oracle: sort, see where a row lands, and the
   value is pinned in a handful of page loads.

Scope note: this is a presentation control over ``/admin/``. The DRF API still
returns every one of these values, and anyone with direct database or shell
access sees them unchanged.
"""

from django import forms
from django.contrib import admin
from django.contrib.admin import helpers
from django.core import checks
from django.core.exceptions import (
    FieldDoesNotExist,
    ImproperlyConfigured,
    ValidationError,
)
from django.db import models
from django.db.models.constants import LOOKUP_SEP
from django.utils.html import format_html
from django.utils.translation import gettext as _

# Fixed length, never len(str(value)) -- the magnitude of a figure is most of
# the secret. Rendered identically whether the stored value is None or not, so
# the box does not disclose null-ness either.
MASK = "*" * 8

# The restore in MaskedAmountFieldMixin._clean_bound_field hangs off a private
# Django hook added in 5.0. If a future upgrade removes it, every blank masked
# box would silently overwrite a stored amount with NULL, so refuse to start
# instead.
if not hasattr(forms.Field, "_clean_bound_field"):
    raise ImproperlyConfigured(
        "core.admin_masking relies on django.forms.Field._clean_bound_field, "
        "which is missing from this Django version. Without it a blank masked "
        "input would overwrite the stored amount with NULL. Move the restore "
        "into a shared ModelForm.clean() before upgrading."
    )


# Fields holding a currency amount, by model. Adding a field here is the only
# edit needed to mask it -- every admin reads this registry.
MASKED_AMOUNT_FIELDS = {
    "accounts.MoneyAccount": ("opening_balance",),
    "accounts.Transaction": ("amount",),
    "accounts.PartyOpeningBalance": ("amount",),
    "root.Expense": ("amount",),
    "root.Customer": ("total_sales",),
    "sales.SalesInvoice": ("sub_total", "total"),
    "sales.SalesInvoiceItem": ("unit_price",),
    "sales.PurchaseInvoice": ("sub_total", "total"),
    "sales.PurchaseInvoiceItem": ("unit_cost",),
    "sales.PurchaseQuotationItem": ("unit_price",),
    "inventory.Inventory": ("net_inventory_value", "total_value_reserved"),
    "inventory.InventoryItem": ("unit_cost", "unit_price"),
}

# Models whose __str__ embeds an amount, so the figure would otherwise show up
# in changelist first columns, FK columns and every <select> that targets them.
# Each entry rebuilds the label from the non-secret attributes; see rule 2.
# The model __str__ methods themselves are left alone -- logs, print() debugging
# and docs/system-test all rely on them.
MASKED_LABELS = {
    "sales.SalesInvoice": (
        lambda obj: f"{obj.id}-{obj.invoice_number}-{MASK}-{obj.status}"
    ),
    "sales.PurchaseInvoice": (
        lambda obj: f"{obj.id}-{obj.invoice_number}-{MASK}-{obj.status}"
    ),
    "accounts.Transaction": (
        lambda obj: f"{obj.id}-{obj.type}-{MASK}-{obj.status}"
    ),
    "accounts.PartyPayment": (
        lambda obj: f"{obj.id}-{obj.customer or obj.supplier}-{MASK}"
    ),
    "accounts.PartyOpeningBalance": (
        lambda obj: f"{obj.id}-{obj.customer or obj.supplier}-{MASK}"
    ),
}


def masked_label(obj):
    """The admin's stand-in for str(obj). Falls through for unlisted models."""
    if obj is None:
        return ""
    builder = MASKED_LABELS.get(obj._meta.label)
    return builder(obj) if builder else str(obj)


class MaskedAmountInput(forms.NumberInput):
    """A number box that renders asterisks and never carries the value."""

    def __init__(self, attrs=None):
        super().__init__(
            {"placeholder": MASK, "autocomplete": "off", **(attrs or {})}
        )

    def get_context(self, name, value, attrs):
        # The value dies here. Widget.format_value(None) returns None, and
        # input.html guards its value attribute on `!= None`, so the rendered
        # tag carries no value attribute at all -- not an empty one.
        #
        # This also clears the box when a form is re-rendered after a
        # validation error, so a rejected submit means retyping the amount.
        # That is the same bargain a password field makes.
        return super().get_context(name, None, attrs)


class MaskedAmountFieldMixin:
    """Restores the stored value when a masked box comes back empty.

    Requiredness is deliberately not handled through ``self.required``. Setting
    ``required=False`` on a NOT NULL model field makes
    ``_get_validation_exclusions`` drop it from ``instance.full_clean()``, so a
    blank add form validates, and ``PurchaseInvoiceItem.save()`` -- which calls
    ``self.full_clean()`` unguarded -- then raises inside ``save_model``, giving
    a 500 page instead of "This field is required.". Keying off ``bf.initial``
    instead gets the add/change distinction right and works inside formsets,
    where ``field.required`` is shared across every row.
    """

    widget = MaskedAmountInput

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # models.Field.formfield() has already set required = not blank.
        # Stash it, then drop required so a blank "keep what is there" submit
        # survives Field.clean() long enough to reach the restore below.
        self.mask_required = self.required
        self.required = False
        # show_hidden_initial renders a second, hidden input carrying the
        # initial value. models.Field.formfield() turns it on for any field
        # with a *callable* default; none of the masked fields has one today,
        # but adding one later would otherwise leak the amount straight back
        # into the page.
        self.show_hidden_initial = False

    def _clean_bound_field(self, bf):
        # Django 5.0+ cleaning hook (forms/forms.py:345). This is the same seam
        # FileField uses at forms/fields.py:708 to reach bf.initial, which on a
        # change form holds the true stored value (model_to_dict, via
        # forms/models.py:367-372).
        if self.disabled:
            return self.clean(bf.initial)

        data = bf.data
        if data in self.empty_values:
            if bf.initial not in self.empty_values:
                # The mask was left in place: keep what is stored.
                return bf.initial
            # Nothing stored to keep. On an add form for a field the model
            # requires, that is a user error -- report it as one.
            if self.mask_required:
                raise ValidationError(
                    self.error_messages["required"], code="required"
                )
        return self.clean(data)

    def has_changed(self, initial, data):
        # An untouched mask is not an edit. Without this, every save would list
        # the amount as changed in the admin history, and extra inline rows
        # would stop qualifying as empty.
        if data in self.empty_values:
            return False
        return super().has_changed(initial, data)


class MaskedFloatField(MaskedAmountFieldMixin, forms.FloatField):
    pass


class MaskedIntegerField(MaskedAmountFieldMixin, forms.IntegerField):
    pass


# Exact-type dispatch, so an unmapped field type raises rather than quietly
# rendering an unmasked box. Both types occur among the masked fields.
MASKED_FORM_FIELDS = {
    models.FloatField: MaskedFloatField,
    models.IntegerField: MaskedIntegerField,
}


def _column(func, name, description):
    """Tag a callable for use as a list_display entry.

    No admin_order_field is set, deliberately (rule 3): admin_list.py:116
    renders no sort link without one, and views/main.py:406-408 ignores a
    hand-crafted ?o= that resolves to it.
    """
    func.__name__ = name  # _coerce_field_name() -> the column's CSS class
    func.short_description = description  # label_for_field() -> column header
    return func


class MaskedAmountsAdminMixin:
    """Put first in the MRO. Needs no per-admin configuration.

    Every masked model reads its own field list out of MASKED_AMOUNT_FIELDS, so
    this same mixin is correct for all 36 registered models -- it is a no-op for
    those in neither registry.
    """

    change_form_template = "admin/masked_change_form.html"

    @property
    def masked_amount_fields(self):
        return MASKED_AMOUNT_FIELDS.get(self.model._meta.label, ())

    # -- change form ------------------------------------------------------

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        # Hooked here rather than in get_form because it lives on
        # BaseModelAdmin, so the same implementation covers future inlines.
        if db_field.name in self.masked_amount_fields:
            form_class = MASKED_FORM_FIELDS.get(type(db_field))
            if form_class is None:
                raise ImproperlyConfigured(
                    f"No masked form field for {type(db_field).__name__} "
                    f"({self.model._meta.label}.{db_field.name}). Add one to "
                    f"MASKED_FORM_FIELDS."
                )
            kwargs.setdefault("form_class", form_class)
            kwargs.setdefault("widget", MaskedAmountInput)
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        formfield = super().formfield_for_foreignkey(db_field, request, **kwargs)
        if formfield is not None:
            builder = MASKED_LABELS.get(db_field.remote_field.model._meta.label)
            if builder is not None:
                # A plain function assigned to the instance is called as
                # builder(obj), not as a bound method.
                formfield.label_from_instance = builder
        return formfield

    def render_change_form(
        self, request, context, add=False, change=False, form_url="", obj=None
    ):
        # options.py:1953 puts str(obj) in the subtitle, and change_form.html:22
        # puts it in the breadcrumb via `original`. Both are replaced here;
        # masked_change_form.html reads masked_original for the breadcrumb.
        context["subtitle"] = masked_label(obj) if obj is not None else None
        context["masked_original"] = masked_label(obj)
        return super().render_change_form(
            request, context, add, change, form_url, obj
        )

    # -- changelist -------------------------------------------------------

    def get_list_display(self, request):
        return [
            self._masked_column(entry) or entry
            for entry in super().get_list_display(request)
        ]

    def _masked_column(self, entry):
        """Cached masked replacement for a list_display entry, or None.

        get_list_display() is called twice per changelist render -- at
        options.py:860 and again via get_sortable_by at :865 -- and the two
        calls must yield the *same* objects, or list_display_links and
        sortable_by membership tests silently mismatch. ModelAdmin instances
        are per-registration singletons, so caching on self is safe.
        """
        if not isinstance(entry, str):
            return None
        try:
            cache = self._masked_columns
        except AttributeError:
            cache = self._masked_columns = {}
        if entry not in cache:
            cache[entry] = self._build_masked_column(entry)
        return cache[entry]

    def _build_masked_column(self, entry):
        opts = self.model._meta

        # Django's implicit default when list_display is unset, which is the
        # case for all 31 bare registrations. It cannot be handled by defining
        # __str__ on the ModelAdmin -- utils.py:297 excludes that name from
        # ModelAdmin-method lookup -- so the entry has to be replaced.
        if entry == "__str__":
            if opts.label not in MASKED_LABELS:
                return None
            return _column(
                lambda obj: masked_label(obj),
                "masked_repr",
                opts.verbose_name,
            )

        if entry in self.masked_amount_fields:
            return _column(
                lambda obj: MASK,
                f"masked_{entry}",
                opts.get_field(entry).verbose_name,
            )

        # An FK column renders the related object, which the template then
        # stringifies. PartyPaymentAdmin.list_display does exactly this with
        # `transaction`, printing the amount on the changelist.
        field = self._masked_fk(entry)
        if field is not None:
            return _column(
                lambda obj, name=entry: masked_label(getattr(obj, name, None)),
                f"masked_{entry}",
                field.verbose_name,
            )

        return None

    def action_checkbox(self, obj):
        # A copy of options.py:1024-1035 with the label masked. Django puts
        # str(obj) into the checkbox's aria-label, which carries the amount onto
        # the changelist even when every visible column is masked -- it is
        # markup rather than a column, so get_list_display never sees it.
        attrs = {
            "class": "action-select",
            "aria-label": format_html(
                _("Select this object for an action - {}"), masked_label(obj)
            ),
        }
        checkbox = forms.CheckboxInput(attrs, lambda value: False)
        return checkbox.render(helpers.ACTION_CHECKBOX_NAME, str(obj.pk))

    def _masked_fk(self, entry):
        try:
            field = self.model._meta.get_field(entry)
        except FieldDoesNotExist:
            return None
        # OneToOneField subclasses ForeignKey but sets many_to_one = False and
        # one_to_one = True, so both flags have to be checked. PartyPayment.
        # transaction is exactly that case.
        if not (field.is_relation and (field.many_to_one or field.one_to_one)):
            return None
        if field.related_model._meta.label not in MASKED_LABELS:
            return None
        return field

    # -- query parameters -------------------------------------------------

    def lookup_allowed(self, lookup, value, request=None):
        # options.py:497-499 returns True for any local-field lookup regardless
        # of list_filter, so ?amount__gt=5000000 filters the changelist today.
        # That is a binary-search oracle: roughly 20 requests pin any masked
        # amount to the rupee without it ever being displayed.
        if lookup.split(LOOKUP_SEP)[0] in self.masked_amount_fields:
            return False
        return super().lookup_allowed(lookup, value, request)


class MaskedModelAdmin(MaskedAmountsAdminMixin, admin.ModelAdmin):
    """Default ModelAdmin for registrations that need no other customisation."""


@checks.register(checks.Tags.admin)
def check_masked_amount_registry(app_configs, **kwargs):
    """Turn a typo in either registry into a startup failure.

    Both registries are keyed by strings, so a renamed model or field would
    otherwise stop being masked in complete silence.
    """
    from django.apps import apps  # deferred: keeps this module model-free

    errors = []

    def resolve(label, error_id):
        try:
            return apps.get_model(label)
        except (LookupError, ValueError):
            errors.append(
                checks.Error(
                    f"admin_masking registry names unknown model '{label}'.",
                    hint="Correct the key or drop the entry.",
                    id=error_id,
                )
            )
            return None

    for label, field_names in MASKED_AMOUNT_FIELDS.items():
        model = resolve(label, "admin_masking.E001")
        if model is None:
            continue
        for name in field_names:
            try:
                field = model._meta.get_field(name)
            except FieldDoesNotExist:
                errors.append(
                    checks.Error(
                        f"MASKED_AMOUNT_FIELDS names unknown field "
                        f"'{label}.{name}'.",
                        id="admin_masking.E002",
                    )
                )
                continue
            if type(field) not in MASKED_FORM_FIELDS:
                errors.append(
                    checks.Error(
                        f"'{label}.{name}' is a {type(field).__name__}, which "
                        f"has no entry in MASKED_FORM_FIELDS.",
                        hint="Add a masked form field class for that type.",
                        id="admin_masking.E003",
                    )
                )

    for label in MASKED_LABELS:
        resolve(label, "admin_masking.E004")

    return errors
