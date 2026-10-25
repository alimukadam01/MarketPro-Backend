"""
The audit trail and the data entry lockout (plans/backlog-data-entry.md, M0).

Requests carry a real JWT rather than force_authenticate: the class under
test is the authentication class, which force_authenticate skips.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from accounts.models import Transaction
from root.models import Business, BusinessConfig, City, Customer, Employee, EmployeeAccess, Expense
from .context import audit_source
from .helpers import audited_update
from .models import AuditEvent

User = get_user_model()


def client_for(user):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'JWT {AccessToken.for_user(user)}')
    return client


class AuditFixture(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(email='audit-owner@example.com', password='pw-for-tests')
        cls.business = Business.objects.create(name='Audit Paints', owner=cls.owner,
                                               phone='03001234567', is_active=True)
        BusinessConfig.objects.create(business=cls.business, accounting=True)
        cls.city = City.objects.create(name='Sukkur', postal_code='65200')
        cls.entry_user = User.objects.create_user(email='entry@example.com', password='pw-for-tests',
                                                  role=User.BACKLOG)
        cls.entry = Employee.objects.create(user=cls.entry_user, business=cls.business,
                                            created_by=cls.owner)
        EmployeeAccess.objects.create(employee=cls.entry, permissions={})


class CaptureTests(AuditFixture):

    def test_a_ui_write_records_the_user_and_the_change(self):
        client = client_for(self.owner)
        response = client.post('/customers/', {'name': 'Hassan Builders', 'city': self.city.id}, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        customer = Customer.objects.get(name='Hassan Builders')

        created = AuditEvent.objects.get(model='root.customer', object_id=str(customer.id), action='create')
        self.assertEqual(created.actor, self.owner)
        self.assertEqual(created.source, 'ui')
        self.assertEqual(created.business_id, self.business.id)
        self.assertEqual(created.changes['name'], [None, 'Hassan Builders'])
        self.assertTrue(created.request_id)

        client.patch(f'/customers/{customer.id}/', {'name': 'Hassan Builders Ltd'}, format='json')
        updated = AuditEvent.objects.get(model='root.customer', object_id=str(customer.id), action='update')
        self.assertEqual(updated.changes, {'name': ['Hassan Builders', 'Hassan Builders Ltd']})

    def test_automatic_work_names_its_source_and_keeps_the_user(self):
        client = client_for(self.owner)
        response = client.post('/expenses/', {'name': 'Rent', 'category': 'kiraya', 'amount': 5000},
                               format='json')
        self.assertEqual(response.status_code, 201, response.content)
        expense = Expense.objects.get(name='Rent')
        mirrored = Transaction.objects.get(expense=expense)
        event = AuditEvent.objects.get(model='accounts.transaction', object_id=str(mirrored.id),
                                       action='create')
        self.assertTrue(event.source.startswith('signal:'), event.source)
        self.assertEqual(event.actor, self.owner)

    def test_no_request_is_the_system(self):
        Customer.objects.create(business=self.business, city=self.city, name='Walk-in')
        event = AuditEvent.objects.filter(model='root.customer', action='create').latest('id')
        self.assertIsNone(event.actor)
        self.assertEqual(event.source, 'system')

    def test_a_queryset_update_is_recorded_per_row(self):
        first = Customer.objects.create(business=self.business, city=self.city, name='A', phone='1')
        second = Customer.objects.create(business=self.business, city=self.city, name='B', phone='2')
        with audit_source('import'):
            count = audited_update(Customer.objects.filter(id__in=[first.id, second.id]), phone='9')
        self.assertEqual(count, 2)
        events = AuditEvent.objects.filter(action='bulk_update', model='root.customer')
        self.assertEqual({e.object_id for e in events}, {str(first.id), str(second.id)})
        self.assertEqual(events.get(object_id=str(first.id)).changes, {'phone': ['1', '9']})
        self.assertTrue(all(e.source == 'import' for e in events))

    def test_an_unchanged_save_records_nothing(self):
        customer = Customer.objects.create(business=self.business, city=self.city, name='Same')
        before = AuditEvent.objects.count()
        customer.save()
        self.assertEqual(AuditEvent.objects.count(), before)

    def test_a_delete_records_what_the_row_held(self):
        customer = Customer.objects.create(business=self.business, city=self.city, name='Gone')
        customer_id = customer.id
        customer.delete()
        event = AuditEvent.objects.get(model='root.customer', object_id=str(customer_id), action='delete')
        self.assertEqual(event.changes['name'], ['Gone', None])


class LockoutTests(AuditFixture):

    def test_data_entry_is_kept_out_of_everything_else(self):
        client = client_for(self.entry_user)
        for url in ('/sales-invoices/', '/customers/', '/sales-kpis/daily-total-sales/',
                    '/global-search/?q=ab', '/audit-events/', '/transactions/', '/targets/'):
            with self.subTest(url):
                self.assertEqual(client.get(url).status_code, 403)
        self.assertEqual(client.post('/customers/', {'name': 'X', 'city': self.city.id}, format='json').status_code, 403)

    def test_data_entry_can_still_sign_in(self):
        client = client_for(self.entry_user)
        me = client.get('/auth/users/me/')
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()['role'], 'backlog')
        self.assertEqual(me.json()['permissions'], {})
        self.assertEqual(client.get('/business/').status_code, 200)
        # Reading its own record is all it may do with it.
        self.assertEqual(client.patch('/auth/users/me/', {'first_name': 'Z'}, format='json').status_code, 403)

    def test_other_roles_are_untouched(self):
        self.assertEqual(client_for(self.owner).get('/sales-invoices/').status_code, 200)


class AuditReadTests(AuditFixture):

    def test_only_the_owner_reads_the_trail(self):
        Customer.objects.create(business=self.business, city=self.city, name='Logged')
        response = client_for(self.owner).get('/audit-events/', {'model': 'root.customer'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any(row['changes'].get('name') == [None, 'Logged'] for row in response.json()))

        staff = User.objects.create_user(email='staff@example.com', password='pw', role=User.EMPLOYEE)
        Employee.objects.create(user=staff, business=self.business, created_by=self.owner)
        self.assertEqual(client_for(staff).get('/audit-events/').status_code, 403)


class EmployeeRoleTests(AuditFixture):

    def create(self, **extra):
        payload = {'first_name': 'New', 'last_name': 'Person', 'password': 'pw-for-tests-1',
                   're_password': 'pw-for-tests-1', 'permissions': {}, **extra}
        return client_for(self.owner).post(
            f'/business/{self.business.id}/employees/create-with-user/', payload, format='json')

    def test_an_employee_is_an_employee_not_an_admin(self):
        response = self.create(email='plain@example.com')
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(User.objects.get(email='plain@example.com').role, User.EMPLOYEE)

    def test_a_data_entry_account(self):
        response = self.create(email='typist@example.com', role='backlog',
                               permissions={'sales': {'view': True}})
        self.assertEqual(response.status_code, 201, response.content)
        user = User.objects.get(email='typist@example.com')
        self.assertEqual(user.role, User.BACKLOG)
        # Module permissions are dropped: the role is the whole access.
        self.assertEqual(user.emp_records.get().access.permissions, {})
