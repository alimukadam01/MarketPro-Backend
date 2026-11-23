"""
People, Phase 4, step 2 of 3: every PartyPayment names its person.

Each existing row names a customer or a supplier; the person is that role's
person. Nothing about money changes: no amount, type, date or status is
touched, so no balance can move. The step checks that every row got a person
and that it is its role's person; any mismatch raises and undoes it.

Old loans recorded with no party have no PartyPayment, so they are not
touched either (register #6).

Reversible: going back clears the column 0007 added.
"""
from django.db import migrations
from django.db.models import F, Q


def name_the_person(apps, schema_editor):
    PartyPayment = apps.get_model('accounts', 'PartyPayment')
    for role in ('customer', 'supplier'):
        for row in (PartyPayment.objects.filter(person__isnull=True, **{f'{role}__isnull': False})
                    .values('pk', f'{role}__person_id')):
            PartyPayment.objects.filter(pk=row['pk']).update(person_id=row[f'{role}__person_id'])

    problems = []
    orphans = PartyPayment.objects.filter(person__isnull=True).count()
    if orphans:
        problems.append(f'{orphans} rows name neither a customer nor a supplier')
    wrong = PartyPayment.objects.filter(
        Q(customer__isnull=False) & ~Q(person_id=F('customer__person_id'))
        | Q(supplier__isnull=False) & ~Q(person_id=F('supplier__person_id'))
    ).count()
    if wrong:
        problems.append(f'{wrong} rows name someone other than their role\'s person')
    if problems:
        raise RuntimeError('PartyPayment person backfill failed its checks; nothing '
                           'was changed. ' + '; '.join(problems))


def clear_the_person(apps, schema_editor):
    apps.get_model('accounts', 'PartyPayment').objects.update(person=None)


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0007_loans_and_set_offs'),
    ]

    operations = [
        migrations.RunPython(name_the_person, clear_the_person),
    ]
