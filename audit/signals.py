"""
The generic capture: every save and delete of a project model.

  post_init   - remembers the row as it was loaded
  post_save   - records what changed against that (all of it on a create),
                then remembers the new state for the next save
  post_delete - records the values the row had

Connected per model class (connect_all), never with sender=None: a data
migration's historical models are other classes, so their writes - which
can run before this table exists - are left alone.
"""
from django.db.models.signals import post_delete, post_init, post_save

from . import recorder

SNAPSHOT = '_audit_snapshot'


def remember(sender, instance, **kwargs):
    # Rows built in memory have no pk yet; their first save is a create.
    if instance.pk is not None:
        instance.__dict__[SNAPSHOT] = recorder.snapshot(instance)


def on_save(sender, instance, created, raw=False, update_fields=None, **kwargs):
    if raw:  # loaddata
        return
    after = recorder.snapshot(instance)
    if created:
        changes = {name: [None, value] for name, value in after.items() if value is not None}
        recorder.record(sender, instance.pk, 'create', changes, recorder.business_of(instance))
    else:
        before = instance.__dict__.get(SNAPSHOT, {})
        if update_fields:
            # Only the columns written can have changed; the rest of the
            # in-memory row may be stale and must not be reported.
            names = {sender._meta.get_field(name).attname for name in update_fields}
            after = {name: value for name, value in after.items() if name in names}
        recorder.record(sender, instance.pk, 'update',
                        recorder.diff(before, after), recorder.business_of(instance))
    instance.__dict__[SNAPSHOT] = {**instance.__dict__.get(SNAPSHOT, {}), **after}


def on_delete(sender, instance, **kwargs):
    values = recorder.snapshot(instance)
    changes = {name: [value, None] for name, value in values.items() if value is not None}
    recorder.record(sender, instance.pk, 'delete', changes, recorder.business_of(instance))


def connect_all(models):
    for model in models:
        uid = f'audit:{model._meta.label_lower}'
        post_init.connect(remember, sender=model, dispatch_uid=uid + ':init', weak=False)
        post_save.connect(on_save, sender=model, dispatch_uid=uid + ':save', weak=False)
        post_delete.connect(on_delete, sender=model, dispatch_uid=uid + ':delete', weak=False)
