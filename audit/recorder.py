"""
Turning a row's before and after into an AuditEvent.

Values are stored JSON-safe: dates and datetimes as ISO text, decimals and
anything else unusual as text, files as their stored name.
"""
import datetime
import decimal
import uuid

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


def business_of(instance_or_values):
    if isinstance(instance_or_values, dict):
        return instance_or_values.get('business_id')
    return getattr(instance_or_values, 'business_id', None)


def record(model, object_id, action, changes, business_id=None):
    """Write one event. Returns it, or None when there is nothing to say."""
    if label(model) in SKIPPED_MODELS:
        return None
    if action in ('update', 'bulk_update') and not changes:
        return None
    from .models import AuditEvent
    actor = context.actor()
    return AuditEvent.objects.create(
        business_id=business_id,
        actor_id=getattr(actor, 'pk', None),
        source=context.source(),
        model=label(model),
        object_id=str(object_id),
        action=action,
        changes=changes,
        request_id=context.request_id(),
    )
