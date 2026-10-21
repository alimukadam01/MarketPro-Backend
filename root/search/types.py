"""
What a search type is, and what one result looks like.

A SearchType is everything global search needs to know about one kind of
record: which model, which fields to match and how, who may see it, and how
to turn a row into a result. Each app declares its own in its `search.py`
(see root/search/__init__.py for how they are found).
"""
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Mapping, Optional, Sequence

from django.utils import timezone

# How a status pill is coloured. The frontend maps these four onto the
# app's existing pill colours, so a new type never needs a frontend change.
TONES = ("success", "warning", "danger", "neutral")


def status(code, label, tone="neutral"):
    """A status for a hit: its code, the label a person reads, and a tone."""
    if tone not in TONES:
        raise ValueError(f'Unknown tone "{tone}".')
    return {"code": code, "label": label, "tone": tone}


def link(path, **state):
    """Where a hit opens: a route, and the location.state that page reads."""
    return {"path": path, "state": state}


def _day(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        value = timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


@dataclass
class Hit:
    """One result, as small as the result list needs."""

    id: int
    title: str
    subtitle: str = ""
    amount: Optional[float] = None
    date: Any = None
    status: Optional[dict] = None
    link: Optional[dict] = None

    def as_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "subtitle": self.subtitle or "",
            "amount": round(float(self.amount), 2) if self.amount is not None else None,
            "date": _day(self.date),
            "status": self.status,
            "link": self.link,
        }


@dataclass
class SearchType:
    """
    One kind of searchable record.

    key, label        - "sales_invoice_item", "Sales invoice items"
    model             - the model searched
    to_hit            - row -> Hit
    module            - the permission module that gates it ("sales",
                        "expenses", ...). None when `access` decides alone,
                        or when the type's own queryset already limits what a
                        user sees (backlog entries).
    access            - optional (request, business, access) -> bool, used
                        instead of the module rule (accounting, targets,
                        admin-only types)
    queryset          - optional (request, business, access) -> QuerySet.
                        Defaults to model.objects.filter(<business_field>=
                        business). Never falls back to every business.
    business_field    - the path to the business, for the default queryset
    text_fields       - matched with icontains
    number_fields     - matched exactly when the query is a number
    phone_fields      - matched on digits only
    choice_fields     - matched on the label a person reads, never the
                        stored code. {field: None} uses the field's own
                        choices; {field: callable} supplies them (a target's
                        data point comes from the targets catalogue).
    select_related, prefetch_related, order_by
    distinct          - set when a field crosses a to-many relation, so one
                        record is not counted once per related row
    prepare           - optional rows -> None, run once on the hits before
                        to_hit (a sales invoice's status needs its khaata
                        settlement attached)
    breakdown         - optional choice field the group is counted by
                        (transactions by type)
    priority          - lower comes first in the result list
    """

    key: str
    label: str
    model: Any
    to_hit: Callable[[Any], Hit]
    module: Optional[str] = None
    access: Optional[Callable] = None
    queryset: Optional[Callable] = None
    business_field: str = "business"
    text_fields: Sequence[str] = ()
    number_fields: Sequence[str] = ()
    phone_fields: Sequence[str] = ()
    choice_fields: Mapping[str, Optional[Callable]] = field(default_factory=dict)
    select_related: Sequence[str] = ()
    prefetch_related: Sequence[str] = ()
    order_by: Sequence[str] = ("-id",)
    distinct: bool = False
    prepare: Optional[Callable] = None
    breakdown: Optional[str] = None
    priority: int = 100

    def base_queryset(self, request, business, access):
        if self.queryset is not None:
            return self.queryset(request, business, access)
        return self.model.objects.filter(**{self.business_field: business})

    def choices(self, field_name):
        """[(code, label)] for a choice field, from the model or the provider."""
        provider = self.choice_fields.get(field_name)
        if provider is not None:
            return list(provider())
        model_field = self.model._meta.get_field(field_name)
        return list(model_field.choices or [])
