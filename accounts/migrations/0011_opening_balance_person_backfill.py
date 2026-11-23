"""
Opening balances move to the person, step 2 of 3: fill person, side and kind.

Each row keeps counting exactly where it counted: on its role's khaata (kind
= the role), with the same signed effect. A positive customer row is a
Receivable, a negative one a Payable (an advance, used by later sales); a
positive supplier row is a Payable, a negative one a Receivable. The amount
becomes its size; the side carries the direction. So no balance can move.

A person may hold only one opening per side. If two rows that count would
land on the same side of one person (a customer row owed to us and a
supplier row of money they hold), the step stops and changes nothing: that
needs a decision from the owner, not a guess. A zero row on a clash is
dropped instead, since it counts for nothing.

Checks afterwards that every row has a person and that each row's signed
effect equals the old signed amount. Reversible: going back restores the
signed amount on the role and clears the new columns.
"""
from collections import defaultdict

from django.db import migrations

NATURAL = {'customer': 'receivable', 'supplier': 'payable'}
OTHER = {'receivable': 'payable', 'payable': 'receivable'}


def to_person(apps, schema_editor):
    Opening = apps.get_model('accounts', 'PartyOpeningBalance')
    planned = []
    for row in Opening.objects.select_related('customer', 'supplier'):
        kind = 'customer' if row.customer_id else 'supplier' if row.supplier_id else None
        if kind is None:
            raise RuntimeError(f'Opening balance {row.pk} names neither a customer nor a '
                               'supplier; nothing was changed.')
        role = row.customer if kind == 'customer' else row.supplier
        amount = row.amount or 0
        side = NATURAL[kind] if amount >= 0 else OTHER[NATURAL[kind]]
        planned.append((row, role.person_id, side, kind, amount))

    by_slot = defaultdict(list)
    for entry in planned:
        by_slot[(entry[1], entry[2])].append(entry)
    dropped, clashes = set(), []
    for (person_id, side), entries in by_slot.items():
        if len(entries) < 2:
            continue
        counting = [entry for entry in entries if entry[4] != 0]
        if len(counting) > 1:
            clashes.append(f'person {person_id} {side}: rows '
                           + ', '.join(str(entry[0].pk) for entry in counting))
            continue
        # Keep the row that counts (or the first, if every one is zero).
        winner = counting[0] if counting else entries[0]
        dropped.update(entry[0].pk for entry in entries if entry is not winner)
    if clashes:
        raise RuntimeError('Two opening balances would land on one side of a person; '
                           'nothing was changed. Decide each with the owner: '
                           + '; '.join(clashes))

    for row, person_id, side, kind, amount in planned:
        if row.pk in dropped:
            Opening.objects.filter(pk=row.pk).delete()
            continue
        Opening.objects.filter(pk=row.pk).update(
            person_id=person_id, side=side, kind=kind, amount=abs(amount))

    problems = []
    for row, person_id, side, kind, amount in planned:
        if row.pk in dropped:
            continue
        stored = Opening.objects.get(pk=row.pk)
        signed = stored.amount if stored.side == NATURAL[stored.kind] else -stored.amount
        if stored.person_id != person_id or signed != amount:
            problems.append(str(row.pk))
    if Opening.objects.filter(person__isnull=True).exists():
        problems.append('rows without a person')
    if problems:
        raise RuntimeError('Opening balance backfill failed its checks; nothing was '
                           'changed: ' + ', '.join(problems))


def to_roles(apps, schema_editor):
    Opening = apps.get_model('accounts', 'PartyOpeningBalance')
    Customer = apps.get_model('root', 'Customer')
    Supplier = apps.get_model('root', 'Supplier')
    for row in Opening.objects.all():
        signed = row.amount if row.side == NATURAL[row.kind] else -row.amount
        Role = Customer if row.kind == 'customer' else Supplier
        role = Role.objects.filter(person_id=row.person_id).first()
        if role is None:
            raise RuntimeError(f'Opening balance {row.pk} has no {row.kind} role to go back to.')
        Opening.objects.filter(pk=row.pk).update(
            amount=signed, person=None, side=None, kind=None, **{row.kind: role})


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0010_opening_balance_person'),
    ]

    operations = [
        migrations.RunPython(to_person, to_roles),
    ]
