"""
Audited versions of the writes that send no model signals.

queryset.update(), bulk_update() and bulk_create() skip post_save, so the
generic capture never sees them. Every such write in the project goes
through one of these instead:

    audited_update(SalesInvoice.objects.filter(pk=pk), status='X')
    audited_bulk_update(InventoryItem, rows, ['quantity'])
    audited_bulk_create(SalesInvoiceItem, rows)

Each reads the affected rows' before-values, does the write exactly as the
plain call would, and records one event per row. They return what the plain
call returns.
"""
from django.db.models.expressions import BaseExpression

from . import recorder


def _names(model, fields):
    return [model._meta.get_field(name).attname for name in fields]


def audited_update(queryset, **fields):
    model = queryset.model
    names = _names(model, fields)
    lookups = ['pk', *names]
    if any(field.name == 'business' for field in model._meta.concrete_fields):
        lookups.append('business_id')
    before = list(queryset.values(*dict.fromkeys(lookups)))
    count = queryset.update(**fields)

    expressions = any(isinstance(value, BaseExpression) for value in fields.values())
    if expressions:
        # An F() or similar: read back what the database worked out.
        after_rows = {row['pk']: row for row in
                      model._default_manager.filter(pk__in=[r['pk'] for r in before]).values('pk', *names)}
    for row in before:
        if expressions:
            after = {name: recorder.safe(after_rows.get(row['pk'], {}).get(name)) for name in names}
        else:
            after = {attname: recorder.safe(value) for attname, value in zip(names, fields.values())}
        old = {name: recorder.safe(row.get(name)) for name in names}
        recorder.record(model, row['pk'], 'bulk_update',
                        recorder.diff(old, after), row.get('business_id'))
    return count


def audited_bulk_update(model, objs, fields, **kwargs):
    objs = list(objs)
    names = _names(model, fields)
    before = {row['pk']: row for row in
              model._default_manager.filter(pk__in=[obj.pk for obj in objs]).values('pk', *names)}
    result = model._default_manager.bulk_update(objs, fields, **kwargs)
    for obj in objs:
        old = {name: recorder.safe(before.get(obj.pk, {}).get(name)) for name in names}
        new = {name: recorder.safe(obj.__dict__.get(name)) for name in names}
        recorder.record(model, obj.pk, 'bulk_update', recorder.diff(old, new), recorder.business_of(obj))
    return result


def audited_bulk_create(model, objs, **kwargs):
    created = model._default_manager.bulk_create(list(objs), **kwargs)
    for obj in created:
        values = recorder.snapshot(obj)
        changes = {name: [None, value] for name, value in values.items() if value is not None}
        recorder.record(model, obj.pk, 'bulk_create', changes, recorder.business_of(obj))
    return created
