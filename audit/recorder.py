"""
Turning a row's before and after into an AuditEvent.

Values are stored JSON-safe: dates and datetimes as ISO text, decimals and
anything else unusual as text, files as their stored name.
"""
import datetime
import decimal
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist
from django.db.models.fields.files import FieldFile

from . import context

# Never audited: the log itself, and Django's own bookkeeping.
SKIPPED_MODELS = {
    'audit.auditevent',
    'sessions.session',
    'contenttypes.contenttype',
    'admin.logentry',
    'auth.permission',
}

# Columns that change on every save and say nothing about what was done.
SKIPPED_FIELDS = {'updated_at', 'last_login'}

# Recorded as changed, never with their values: the trail is read by the
# owner, and a password hash is no one's business but its user's.
HIDDEN_FIELDS = {'password'}
HIDDEN = '(hidden)'

# How far business_of looks through related records. The deepest real chain
# is two (EmployeeAccess -> Employee -> Business).
MAX_DEPTH = 4


def hide(pair):
    """[before, after] with each value that was there replaced by HIDDEN."""
    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
        return HIDDEN
    return [None if value is None else HIDDEN for value in pair]


def safe(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (decimal.Decimal, uuid.UUID)):
        return str(value)
    if isinstance(value, FieldFile):
        return value.name or None
    if isinstance(value, (list, tuple)):
        return [safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): safe(item) for key, item in value.items()}
    return str(value)


def tracked_fields(model):
    """The concrete columns, by attname (customer_id, not customer)."""
    return [
        field.attname for field in model._meta.concrete_fields
        if field.attname not in SKIPPED_FIELDS
    ]


def snapshot(instance):
    return {name: safe(instance.__dict__.get(name)) for name in tracked_fields(type(instance))}


def diff(before, after):
    return {
        name: [before.get(name), value]
        for name, value in after.items()
        if before.get(name) != value
    }


def label(model):
    return model._meta.label_lower


# ── Which business a record belongs to ──────────────────────────────────────
#
# 23 of the audited models have no business column: a payment is on an
# invoice, a variant under a product, a review on a backlog page. Their
# events were stored with no business, and the owner's trail - filtered by
# business - never showed them.


def business_of(instance):
    """The business a record belongs to: its own business_id, or else the
    nearest one among the records it points to, and theirs. None when no
    path leads to one (a city, a unit: shared lists)."""
    found = _own_business(instance)
    if found:
        return found
    return _search([instance], 1, {(type(instance), instance.pk)})


def _search(level, depth, seen):
    """Looks through the records `level` points to, nearest first: every
    record one step away is checked before any two steps away, so a payment
    takes its invoice's business and never wanders off through the
    invoice's customer."""
    if depth > MAX_DEPTH:
        return None
    next_level = []
    for obj in level:
        for related in _related(obj):
            key = (type(related), related.pk)
            if key in seen:
                continue
            seen.add(key)
            found = _own_business(related)
            if found:
                return found
            next_level.append(related)
    return _search(next_level, depth + 1, seen) if next_level else None


def _own_business(obj):
    from root.models import Business
    if isinstance(obj, Business):
        return obj.pk
    return getattr(obj, 'business_id', None)


def _carries_business(model):
    from root.models import Business
    return model is Business or any(f.attname == 'business_id' for f in model._meta.concrete_fields)


def _related(obj):
    """The single records `obj` leads to.

    Followed: every foreign key and one-to-one it holds, and reverse
    one-to-ones. Not followed: reverse many-links - a city's customers, a
    user's invoices - which fan out into many records, possibly in other
    businesses, so taking the first would file the event under the wrong
    shop. A user stands in for the business they work in, the rule
    get_active_business uses.

    Each is read from the id `obj` holds, so a child's delete still finds
    its parent: in a cascade Django deletes children first. Records already
    loaded on `obj` cost no query.
    """
    if isinstance(obj, get_user_model()):
        business = context.user_business(obj)
        if business:
            yield business
        return
    forward = [f for f in obj._meta.concrete_fields
               if f.is_relation and (f.many_to_one or f.one_to_one) and getattr(obj, f.attname) is not None]
    # Those that lead straight to a business first: usually the first answers.
    forward.sort(key=lambda f: not _carries_business(f.related_model))
    for field in forward:
        try:
            yield getattr(obj, field.name)
        except ObjectDoesNotExist:
            continue
    for relation in obj._meta.related_objects:
        if relation.one_to_one:
            try:
                yield getattr(obj, relation.get_accessor_name())
            except ObjectDoesNotExist:
                continue


# The events of a batched() block, waiting to be written together.
_pending = ContextVar('audit_pending', default=None)


@contextmanager
def batched():
    """Write the events recorded inside this block in one INSERT at its end.

    A 20-line invoice records some 80 events, one INSERT each. They are the
    same events, in the same order, with the same values; only their
    created_at is the end of the block rather than the moment of each write,
    a few milliseconds later (ordering ties fall to the id, which keeps the
    order they were recorded in).

    Use it INSIDE the transaction.atomic() of the save, so the events are
    written in that transaction: if the block raises they are dropped, just as
    the rolled-back writes they describe. Not around code that swallows an
    exception from an inner savepoint - those writes are rolled back, but
    their events would still be written. Nested blocks write at the
    outermost one's end.
    """
    if _pending.get() is not None:
        yield
        return
    pending = []
    token = _pending.set(pending)
    try:
        yield
    finally:
        _pending.reset(token)
    # Reached only when the block did not raise.
    if pending:
        from .models import AuditEvent
        AuditEvent.objects.bulk_create(pending)


def record(model, object_id, action, changes, business_id=None):
    """Write one event. Returns it, or None when there is nothing to say.

    With no business found for the record, the event takes the business of
    the person making the change: a city or a unit belongs to no shop, but it
    was added by someone working in one.
    """
    if label(model) in SKIPPED_MODELS:
        return None
    if action in ('update', 'bulk_update') and not changes:
        return None
    from .models import AuditEvent
    changes = {name: hide(value) if name in HIDDEN_FIELDS else value for name, value in changes.items()}
    actor_id = getattr(context.actor(), 'pk', None)
    if actor_id is None and context.request_id() and model is get_user_model():
        # A request with nobody signed in writing a user: someone signing up,
        # or resetting their own password. They are the one acting.
        actor_id = object_id
    event = AuditEvent(
        business_id=business_id or context.business_id(),
        actor_id=actor_id,
        source=context.source(),
        model=label(model),
        object_id=str(object_id),
        action=action,
        changes=changes,
        request_id=context.request_id(),
    )
    pending = _pending.get()
    if pending is not None:
        pending.append(event)
    else:
        event.save(force_insert=True)
    return event
