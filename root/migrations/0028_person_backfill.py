"""
People, step 2 of 3: give every customer and every supplier a Person.

For each customer, one Person with the customer's name, phone, email,
address, city and notes. For each supplier, one Person with the supplier's
name, business name, phone, email and notes; suppliers never had a city or
an address. People who were both a customer and a supplier get two Persons
here and are joined later through the merge tool, never automatically
(register R6, #16).

Suppliers also get the creation date they never had, in this order
(register S6): the audit log's "create" event, else their earliest purchase
invoice, else now.

Nothing about money is touched, so no balance can change. The step checks
its own work instead: every role has a person, there are exactly as many
people as roles, and every copied value matches. Any mismatch raises, and
the migration is undone as one unit.

Not reversible. Step 3 drops the old columns; on production, restoring the
snapshot taken before the release is the way back.
"""
from django.db import migrations
from django.db.models import F
from django.utils import timezone


CUSTOMER_FIELDS = ('name', 'phone', 'email', 'address', 'city_id', 'notes')
SUPPLIER_FIELDS = ('name', 'business_name', 'phone', 'email', 'notes')


def supplier_created_at(AuditEvent, PurchaseInvoice, supplier_id, now):
    audited = (
        AuditEvent.objects
        .filter(model='root.supplier', object_id=str(supplier_id),
                action__in=('create', 'bulk_create'))
        .order_by('created_at')
        .values_list('created_at', flat=True)
        .first()
    )
    if audited:
        return audited
    first_invoice = (
        PurchaseInvoice.objects
        .filter(supplier_id=supplier_id)
        .order_by('date_issued')
        .values_list('date_issued', flat=True)
        .first()
    )
    return first_invoice or now


def give_everyone_a_person(apps, schema_editor):
    Person = apps.get_model('root', 'Person')
    Customer = apps.get_model('root', 'Customer')
    Supplier = apps.get_model('root', 'Supplier')
    AuditEvent = apps.get_model('audit', 'AuditEvent')
    PurchaseInvoice = apps.get_model('sales', 'PurchaseInvoice')
    now = timezone.now()

    people_before = Person.objects.count()

    for customer in Customer.objects.filter(person__isnull=True).order_by('id'):
        person = Person.objects.create(
            business_id=customer.business_id,
            **{field: getattr(customer, field) for field in CUSTOMER_FIELDS})
        # The person has existed as long as the customer has.
        Person.objects.filter(pk=person.pk).update(
            created_at=customer.created_at, updated_at=customer.updated_at)
        Customer.objects.filter(pk=customer.pk).update(person_id=person.pk)

    for supplier in Supplier.objects.filter(person__isnull=True).order_by('id'):
        created_at = supplier_created_at(
            AuditEvent, PurchaseInvoice, supplier.id, now)
        person = Person.objects.create(
            business_id=supplier.business_id,
            **{field: getattr(supplier, field) for field in SUPPLIER_FIELDS})
        Person.objects.filter(pk=person.pk).update(
            created_at=created_at, updated_at=created_at)
        Supplier.objects.filter(pk=supplier.pk).update(
            person_id=person.pk, created_at=created_at)

    # ── Checks: any failure undoes the whole migration. ──────────────────────
    problems = []
    customers = Customer.objects.count()
    suppliers = Supplier.objects.count()
    people_added = Person.objects.count() - people_before
    if people_added != customers + suppliers:
        problems.append(
            f'{people_added} people added for {customers} customers '
            f'and {suppliers} suppliers')
    for Role, label in ((Customer, 'customers'), (Supplier, 'suppliers')):
        missing = Role.objects.filter(person__isnull=True).count()
        if missing:
            problems.append(f'{missing} {label} have no person')
        mismatched = Role.objects.exclude(business_id=F('person__business_id'))
        if mismatched.exists():
            problems.append(f'{mismatched.count()} {label} in another business than their person')
    if Supplier.objects.filter(created_at__isnull=True).exists():
        problems.append('suppliers without a creation date')

    for customer in Customer.objects.select_related('person'):
        for field in CUSTOMER_FIELDS:
            if getattr(customer, field) != getattr(customer.person, field):
                problems.append(f'customer {customer.pk}: {field} differs')
    for supplier in Supplier.objects.select_related('person'):
        for field in SUPPLIER_FIELDS:
            if getattr(supplier, field) != getattr(supplier.person, field):
                problems.append(f'supplier {supplier.pk}: {field} differs')

    if problems:
        raise RuntimeError(
            'People backfill failed its checks; nothing was changed. '
            + '; '.join(problems[:20]))


class Migration(migrations.Migration):

    dependencies = [
        ('root', '0027_person'),
        ('audit', '0001_initial'),
        ('sales', '0035_discount_never_empty'),
    ]

    operations = [
        migrations.RunPython(give_everyone_a_person),
    ]
