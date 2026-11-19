"""
People, Phase 3: the /people/ API (register #29, R1-R35, S1-S11).

One person, two sides: Receivable (a customer who owes, a supplier holding
the business's money) and Payable (a supplier owed, a customer's advance).
Roles are deactivated, never deleted; only a person is deleted, after a
preview of everything that goes with them, confirmed by typing the name.
"""
import datetime
import importlib

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from accounts.models import MoneyAccount, PartyOpeningBalance, PartyPayment, Transaction
from projects.models import Project
from sales.models import PurchaseInvoice, PurchaseReceipt, SalesInvoice, SalesReceipt

from .models import (
    WALK_IN_CUSTOMER_NAME, Business, City, Customer, Employee, EmployeeAccess,
    Person, Supplier,
)
from .people import normalize_phone

User = get_user_model()


def client_for(user):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'JWT {AccessToken.for_user(user)}')
    return client


def noon(y, m, d):
    return timezone.make_aware(datetime.datetime(y, m, d, 12))


class PeopleAPIFixture(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.city = City.objects.create(name='Karachi', postal_code='74000')
        cls.owner = User.objects.create_user(email='people-api@example.com', password='pw-for-tests')
        cls.business = Business.objects.create(name='Ali Paints', owner=cls.owner,
                                               phone='03001234567', is_active=True)
        cls.account = MoneyAccount.objects.get_default_account(cls.business.id)

    def setUp(self):
        self.client = client_for(self.owner)

    # ── helpers ──
    def customer(self, name, phone=None, **kwargs):
        return Customer.objects.create(business=self.business, name=name, phone=phone,
                                       city=self.city, **kwargs)

    def supplier(self, name, phone=None, person=None):
        if person is not None:
            return Supplier.objects.create(business=self.business, person=person)
        return Supplier.objects.create(business=self.business, name=name, phone=phone)

    def sale(self, customer, total, number='SI-1', day=(2026, 3, 1)):
        return SalesInvoice.objects.create(
            business=self.business, customer=customer, created_by=self.owner,
            invoice_number=number, total=total, sub_total=total, status='C',
            tax={'value': 0, 'type': 'percentage'}, discount={'value': 0, 'type': 'amount'},
            date_issued=noon(*day))

    def purchase(self, supplier, total, number='PI-1', day=(2026, 3, 5)):
        return PurchaseInvoice.objects.create(
            business=self.business, supplier=supplier, created_by=self.owner,
            invoice_number=number, total=total, sub_total=total, status='R',
            is_restocked=True, tax={'value': 0, 'type': 'percentage'}, date_issued=noon(*day))

    def on_account(self, txn_type, amount, party, day=(2026, 3, 10)):
        txn = Transaction.objects.create(
            business=self.business, account=self.account, type=txn_type, amount=amount,
            date=datetime.date(*day), status='C', payment_method='cash')
        kind = 'customer' if isinstance(party, Customer) else 'supplier'
        PartyPayment.objects.create(business=self.business, transaction=txn, **{kind: party})
        return txn

    def row(self, person_id, **params):
        rows = self.client.get('/people/', params).json()
        return next((r for r in rows if r['id'] == person_id), None)


class PeopleListTests(PeopleAPIFixture):

    def test_a_person_with_both_roles_shows_two_sides_and_a_net(self):
        ali = self.customer('Ali Traders', '0300-1234567')
        ali_supplier = self.supplier('', person=ali.person)
        self.sale(ali, 30000)
        self.purchase(ali_supplier, 50000)
        row = self.row(ali.person_id)
        self.assertEqual((row['receivable'], row['payable'], row['net']), (30000, 50000, -20000))
        self.assertEqual(row['roles']['customer'], {'id': ali.id, 'is_active': True})
        self.assertEqual(row['roles']['supplier'], {'id': ali_supplier.id, 'is_active': True})
        self.assertEqual(row['last_activity'], '2026-03-05')

    def test_a_customer_advance_is_payable(self):
        fatima = self.customer('Fatima Interiors')
        self.on_account('customer_receipt', 10000, fatima)
        row = self.row(fatima.person_id)
        self.assertEqual((row['receivable'], row['payable']), (0, 10000))

    def test_filters_by_role_and_side(self):
        ali = self.customer('Ali Traders')
        self.sale(ali, 30000)
        bashir = self.supplier('Bashir Hardware')
        self.purchase(bashir, 300000)
        nobody = Person.objects.create(business=self.business, name='Rafiq Ahmed')

        def ids(**params):
            return {r['id'] for r in self.client.get('/people/', params).json()}

        self.assertIn(ali.person_id, ids(role='customer'))
        self.assertNotIn(bashir.person_id, ids(role='customer'))
        self.assertEqual(ids(role='none'), {nobody.id})
        self.assertEqual(ids(side='payable'), {bashir.person_id})
        self.assertIn(ali.person_id, ids(side='receivable'))
        self.assertEqual(ids(search='Bashir'), {bashir.person_id})


class PersonCreateAndEditTests(PeopleAPIFixture):

    def test_create_with_roles_and_an_opening_balance_per_side(self):
        response = self.client.post('/people/', {
            'name': 'Zubair Khan', 'phone': '0301-9902215', 'city': self.city.id,
            'roles': ['customer', 'supplier'],
            'opening_balances': {
                'receivable': {'amount': 40000, 'as_of_date': '2026-01-01'},
                'payable': {'amount': 100000, 'as_of_date': '2026-01-01'},
            },
        }, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual((body['receivable'], body['payable']), (40000, 100000))
        self.assertEqual(body['opening_balances']['receivable']['amount'], 40000)
        person = Person.objects.get(pk=body['id'])
        self.assertEqual(person.customer.opening_balance.amount, 40000)
        self.assertEqual(person.supplier.opening_balance.amount, 100000)

    def test_a_person_with_no_role_is_allowed(self):
        response = self.client.post('/people/', {'name': 'Kamran Iqbal'}, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()['roles'], {'customer': None, 'supplier': None})

    def test_an_opening_balance_needs_the_role_for_its_side(self):
        response = self.client.post('/people/', {
            'name': 'X', 'roles': ['supplier'],
            'opening_balances': {'receivable': {'amount': 5, 'as_of_date': '2026-01-01'}},
        }, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('opening_balances', response.json())

    def test_editing_details_changes_them_for_every_role(self):
        ali = self.customer('Ali Traders')
        supplier = self.supplier('', person=ali.person)
        response = self.client.patch(f'/people/{ali.person_id}/', {'phone': '0300-7654321'}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        supplier.person.refresh_from_db()
        self.assertEqual(supplier.phone, '0300-7654321')


class RoleChangeTests(PeopleAPIFixture):

    def roles(self, person, role, action):
        return self.client.post(f'/people/{person.id}/roles/', {'role': role, 'action': action},
                                format='json')

    def test_adding_a_role(self):
        person = Person.objects.create(business=self.business, name='Shahid Paints')
        response = self.roles(person, 'supplier', 'add')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(Supplier.objects.filter(person=person).exists())
        self.assertEqual(self.roles(person, 'supplier', 'add').status_code, 400)

    def test_a_role_with_a_balance_cannot_be_deactivated(self):
        ali = self.customer('Ali Traders')
        self.sale(ali, 30000)
        response = self.roles(ali.person, 'customer', 'deactivate')
        self.assertEqual(response.status_code, 400)
        self.assertIn('Receivable balance of PKR 30,000', response.json()['detail'])
        ali.refresh_from_db()
        self.assertTrue(ali.is_active)

    def test_a_settled_role_is_deactivated_and_leaves_the_pickers(self):
        ali = self.customer('Ali Traders')
        response = self.roles(ali.person, 'customer', 'deactivate')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(response.json()['roles']['customer']['is_active'])
        listed = [c['id'] for c in self.client.get('/customers/').json()]
        self.assertNotIn(ali.id, listed)
        everyone = [c['id'] for c in self.client.get('/customers/', {'include_inactive': 'true'}).json()]
        self.assertIn(ali.id, everyone)
        self.assertEqual(self.client.get(f'/customers/{ali.id}/').status_code, 200)
        self.assertEqual(self.roles(ali.person, 'customer', 'activate').status_code, 200)

    def test_the_walk_in_customer_stays_active(self):
        walk_in = Customer.objects.get(business=self.business, person__name=WALK_IN_CUSTOMER_NAME)
        self.assertEqual(self.roles(walk_in.person, 'customer', 'deactivate').status_code, 400)


class PersonDeleteTests(PeopleAPIFixture):

    def setUp(self):
        super().setUp()
        self.ali = self.customer('Ali Traders', '0300-1234567')
        self.ali_supplier = self.supplier('', person=self.ali.person)
        self.sale_invoice = self.sale(self.ali, 50000)
        SalesReceipt.objects.create(sales_invoice=self.sale_invoice, amount=20000)
        self.purchase_invoice = self.purchase(self.ali_supplier, 80000)
        PurchaseReceipt.objects.create(purchase_invoice=self.purchase_invoice, amount=30000)
        self.received = self.on_account('customer_receipt', 5000, self.ali)
        PartyOpeningBalance.objects.create(business=self.business, customer=self.ali,
                                           amount=15000, as_of_date=datetime.date(2026, 1, 1))
        self.project = Project.objects.create(business=self.business, name='Shop fit-out',
                                              customer=self.ali)
        self.person = self.ali.person

    def preview(self):
        response = self.client.get(f'/people/{self.person.id}/delete-preview/')
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_the_preview_lists_everything_that_goes(self):
        preview = self.preview()
        deleted = {row['model']: row for row in preview['deleted']}
        self.assertEqual(deleted['root.person']['count'], 1)
        self.assertEqual(deleted['root.customer']['count'], 1)
        self.assertEqual(deleted['root.supplier']['count'], 1)
        self.assertEqual((deleted['sales.salesinvoice']['count'], deleted['sales.salesinvoice']['total']),
                         (1, 50000))
        self.assertEqual(deleted['sales.purchaseinvoice']['total'], 80000)
        self.assertEqual(deleted['accounts.partyopeningbalance']['total'], 15000)
        # The two invoice payments and the on-account payment.
        self.assertEqual(deleted['accounts.transaction']['count'], 3)
        kept = {row['model']: row for row in preview['kept']}
        self.assertEqual(kept['projects.project']['count'], 1)
        # Cash in: +20,000 +5,000 received, -30,000 paid out: deleting them
        # moves the account by the opposite, +5,000.
        account = preview['money_accounts'][0]
        self.assertEqual(account['change'], 5000)
        self.assertEqual(account['balance_after'] - account['balance_now'], 5000)
        self.assertTrue(preview['can_delete'])

    def test_delete_needs_the_name_typed(self):
        response = self.client.delete(f'/people/{self.person.id}/', {'confirm_name': 'Ali'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertTrue(Person.objects.filter(pk=self.person.pk).exists())

    def test_delete_removes_what_the_preview_listed_transactions_included(self):
        preview = self.preview()
        before = MoneyAccount.objects.filter(pk=self.account.pk).with_balance().get().balance_value
        response = self.client.delete(f'/people/{self.person.id}/',
                                      {'confirm_name': '  ali traders '}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(Person.objects.filter(pk=self.person.pk).exists())
        self.assertFalse(Customer.objects.filter(pk=self.ali.pk).exists())
        self.assertFalse(SalesInvoice.objects.filter(pk=self.sale_invoice.pk).exists())
        self.assertFalse(PurchaseInvoice.objects.filter(pk=self.purchase_invoice.pk).exists())
        self.assertFalse(Transaction.objects.filter(pk=self.received.pk).exists())
        self.project.refresh_from_db()
        self.assertIsNone(self.project.customer_id)
        after = MoneyAccount.objects.filter(pk=self.account.pk).with_balance().get().balance_value
        self.assertEqual(after - before, preview['money_accounts'][0]['change'])

    def test_the_walk_in_customer_cannot_be_deleted(self):
        walk_in = Customer.objects.get(business=self.business, person__name=WALK_IN_CUSTOMER_NAME)
        self.assertFalse(self.client.get(f'/people/{walk_in.person_id}/delete-preview/').json()['can_delete'])
        response = self.client.delete(f'/people/{walk_in.person_id}/',
                                      {'confirm_name': WALK_IN_CUSTOMER_NAME}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_bulk_delete_only_takes_people_with_no_records(self):
        empty = Person.objects.create(business=self.business, name='Typo Person')
        refused = self.client.post('/people/bulk-delete/', {'person_ids': [empty.id, self.person.id]},
                                   format='json')
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(refused.json()['refused'], ['Ali Traders'])
        self.assertTrue(Person.objects.filter(pk=empty.pk).exists())
        done = self.client.post('/people/bulk-delete/', {'person_ids': [empty.id]}, format='json')
        self.assertEqual(done.status_code, 200)
        self.assertFalse(Person.objects.filter(pk=empty.pk).exists())


class DuplicatesAndMergeTests(PeopleAPIFixture):

    def test_phones_are_compared_in_any_format(self):
        self.assertEqual(normalize_phone('0300-1234567'), '03001234567')
        self.assertEqual(normalize_phone('+92 300 1234567'), '03001234567')
        self.assertEqual(normalize_phone('923001234567'), '03001234567')
        self.assertEqual(normalize_phone('3001234567'), '03001234567')
        self.assertEqual(normalize_phone('-'), '')

    def test_duplicates_are_suggested_by_phone(self):
        first = self.customer('Ali Traders', '0300-1234567')
        second = self.supplier('Ali', '+92 300 1234567')
        self.customer('Someone Else', '0321-0000000')
        groups = self.client.get('/people/duplicates/').json()
        self.assertEqual(len(groups), 1)
        self.assertEqual({p['id'] for p in groups[0]['people']}, {first.person_id, second.person_id})

    def test_merging_a_customer_and_a_supplier_joins_them(self):
        customer = self.customer('Ali Traders', '0300-1234567')
        supplier = self.supplier('Ali', '+92 300 1234567')
        invoice = self.purchase(supplier, 50000)
        response = self.client.post(f'/people/{customer.person_id}/merge/', {
            'other_id': supplier.person_id, 'name': 'Ali Traders', 'phone': '0300-1234567',
        }, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        supplier.refresh_from_db()
        self.assertEqual(supplier.person_id, customer.person_id)
        self.assertEqual(supplier.id, PurchaseInvoice.objects.get(pk=invoice.pk).supplier_id)
        self.assertFalse(Person.objects.filter(name='Ali').exists())
        self.assertEqual(response.json()['payable'], 50000)

    def test_merging_two_customers_moves_everything_onto_the_kept_one(self):
        kept = self.customer('Hamza Furniture', '0333-4410087')
        other = self.customer('Hamza', '03334410087')
        invoice = self.sale(other, 25000)
        self.on_account('customer_receipt', 5000, other)
        response = self.client.post(f'/people/{kept.person_id}/merge/',
                                    {'other_id': other.person_id}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(SalesInvoice.objects.get(pk=invoice.pk).customer_id, kept.id)
        self.assertFalse(Customer.objects.filter(pk=other.pk).exists())
        self.assertEqual(response.json()['receivable'], 20000)

    def test_two_opening_balances_on_one_side_block_the_merge(self):
        kept = self.customer('Hamza Furniture')
        other = self.customer('Hamza')
        for role in (kept, other):
            PartyOpeningBalance.objects.create(business=self.business, customer=role,
                                               amount=1000, as_of_date=datetime.date(2026, 1, 1))
        response = self.client.post(f'/people/{kept.person_id}/merge/',
                                    {'other_id': other.person_id}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('opening balance', response.json()['detail'])
        self.assertTrue(Customer.objects.filter(pk=other.pk).exists())


class PeoplePermissionTests(PeopleAPIFixture):

    def employee(self, people):
        user = User.objects.create_user(email=f'staff-{len(str(people))}@example.com',
                                        password='pw', role=User.EMPLOYEE)
        employee = Employee.objects.create(user=user, business=self.business, created_by=self.owner)
        EmployeeAccess.objects.create(employee=employee, permissions={'people': people})
        return client_for(user)

    def test_no_people_access_is_refused(self):
        client = self.employee({'view': False})
        self.assertEqual(client.get('/people/').status_code, 403)

    def test_viewing_does_not_allow_deleting(self):
        person = Person.objects.create(business=self.business, name='Kept')
        client = self.employee({'view': True, 'create': False, 'edit': False, 'delete': False})
        self.assertEqual(client.get('/people/').status_code, 200)
        self.assertEqual(client.delete(f'/people/{person.id}/', {'confirm_name': 'Kept'},
                                       format='json').status_code, 403)
        self.assertEqual(client.post(f'/people/{person.id}/merge/', {'other_id': person.id},
                                     format='json').status_code, 403)


class PeoplePermissionMigrationTests(TestCase):
    """
    root/migrations/0030: view, create and edit on People = customers OR
    suppliers; delete is never granted, the owner gives it by hand.
    """

    def test_people_is_granted_from_customers_or_suppliers_except_delete(self):
        owner = User.objects.create_user(email='mig-owner@example.com', password='pw')
        business = Business.objects.create(name='B', owner=owner, phone='1', is_active=True)
        staff = User.objects.create_user(email='mig-staff@example.com', password='pw', role=User.EMPLOYEE)
        employee = Employee.objects.create(user=staff, business=business, created_by=owner)
        access = EmployeeAccess.objects.create(employee=employee, permissions={
            'customers': {'view': True, 'create': True, 'edit': False, 'delete': True},
            'suppliers': {'view': True, 'create': False, 'edit': True, 'delete': True},
        })
        migration = importlib.import_module('root.migrations.0030_people_permission')
        migration.grant(django_apps, None)
        access.refresh_from_db()
        self.assertEqual(access.permissions['people'],
                         {'view': True, 'create': True, 'edit': True, 'delete': False})
        migration.revoke(django_apps, None)
        access.refresh_from_db()
        self.assertNotIn('people', access.permissions)
