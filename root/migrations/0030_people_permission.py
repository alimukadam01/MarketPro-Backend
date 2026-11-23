"""
People: give every employee a `people` permission row (register Q7).

View, create and edit are granted if the employee had them on customers or
on suppliers, so nobody loses access to someone they could see before.
Delete is never granted here: deleting a person removes their invoices and
transactions, so the owner gives it by hand to whoever should have it
(owner, 2026-11-19). Admins are not stored here; they have every module. An
existing `people` row is left alone.

Reversible: there was no `people` module before this, so going back removes
the rows.
"""
from django.db import migrations

# Delete is left out on purpose; see the docstring.
GRANTED = ('view', 'create', 'edit')


def grant(apps, schema_editor):
    EmployeeAccess = apps.get_model('root', 'EmployeeAccess')
    for access in EmployeeAccess.objects.all():
        permissions = dict(access.permissions or {})
        if 'people' in permissions:
            continue
        customers = permissions.get('customers') or {}
        suppliers = permissions.get('suppliers') or {}
        permissions['people'] = {
            action: bool(customers.get(action)) or bool(suppliers.get(action))
            for action in GRANTED
        }
        permissions['people']['delete'] = False
        access.permissions = permissions
        access.save(update_fields=['permissions'])


def revoke(apps, schema_editor):
    EmployeeAccess = apps.get_model('root', 'EmployeeAccess')
    for access in EmployeeAccess.objects.all():
        permissions = dict(access.permissions or {})
        if permissions.pop('people', None) is not None:
            access.permissions = permissions
            access.save(update_fields=['permissions'])


class Migration(migrations.Migration):

    dependencies = [
        ('root', '0029_person_finish'),
    ]

    operations = [
        migrations.RunPython(grant, revoke),
    ]
