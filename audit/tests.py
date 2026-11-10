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
from . import context
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


class BusinessSearchTests(AuditFixture):
    """Records with no business column take one from the records they point
    to (recorder.business_of), so the owner's trail shows them."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from root.models import Product, ProductVariant, Unit
        from sales.models import SalesInvoice
        cls.customer = Customer.objects.create(business=cls.business, city=cls.city, name='Hassan')
        cls.invoice = SalesInvoice.objects.create(
            business=cls.business, customer=cls.customer, created_by=cls.owner,
            status='D', invoice_number='AUD-1')
        cls.unit = Unit.objects.create(name='Tin', abv='Tin')
        cls.product = Product.objects.create(business=cls.business, name='Emulsion', unit=cls.unit)
        cls.variant = ProductVariant.objects.create(base=cls.product, name='White', sku='AUD-W')

    def event(self, model, object_id, action='create'):
        return AuditEvent.objects.filter(model=model, object_id=str(object_id), action=action).latest('id')

    def test_a_payment_takes_its_invoices_business(self):
        from sales.models import SalesReceipt
        receipt = SalesReceipt.objects.create(sales_invoice=self.invoice, amount=500)
        self.assertEqual(self.event('sales.salesreceipt', receipt.id).business_id, self.business.id)

    def test_a_variant_takes_its_products_business(self):
        self.assertEqual(self.event('root.productvariant', self.variant.id).business_id, self.business.id)

    def test_an_image_takes_whichever_invoice_it_is_on(self):
        from root.models import Supplier
        from sales.models import InvoiceImage, PurchaseInvoice
        supplier = Supplier.objects.create(business=self.business, name='Jotun')
        bill = PurchaseInvoice.objects.create(business=self.business, supplier=supplier,
                                              created_by=self.owner, status='D')
        # A stored name, not a file: nothing is uploaded.
        on_sale = InvoiceImage.objects.create(sales_invoice=self.invoice, image='invoices/a.png')
        on_purchase = InvoiceImage.objects.create(purchase_invoice=bill, image='invoices/b.png')
        self.assertEqual(self.event('sales.invoiceimage', on_sale.id).business_id, self.business.id)
        self.assertEqual(self.event('sales.invoiceimage', on_purchase.id).business_id, self.business.id)

    def test_through_a_record_that_has_none_either(self):
        access = EmployeeAccess.objects.get(employee=self.entry)
        access.permissions = {'sales': {'view': True}}
        access.save()
        self.assertEqual(self.event('root.employeeaccess', access.id, 'update').business_id, self.business.id)

    def test_the_nearest_business_wins(self):
        from backlog.models import BacklogEntry, BacklogReview
        # The reviewer owns another, active business. That is two steps away
        # (review -> user -> their business); the page is one.
        other_owner = User.objects.create_user(email='other@example.com', password='pw')
        Business.objects.create(name='Other Shop', owner=other_owner, phone='03007654321', is_active=True)
        page = BacklogEntry.objects.create(business=self.business, type='sales_invoice',
                                           created_by=self.owner, image='backlog/p.png')
        review = BacklogReview.objects.create(entry=page, reviewer=other_owner, entered_by=other_owner,
                                              outcome=BacklogReview.APPROVED)
        self.assertEqual(self.event('backlog.backlogreview', review.id).business_id, self.business.id)

    def test_a_delete_still_finds_the_parent(self):
        from sales.models import SalesReceipt
        receipt = SalesReceipt.objects.create(sales_invoice=self.invoice, amount=250)
        receipt_id = receipt.id
        receipt.delete()
        self.assertEqual(self.event('sales.salesreceipt', receipt_id, 'delete').business_id, self.business.id)

    def test_a_bulk_update_finds_it_too(self):
        from sales.models import SalesReceipt
        receipt = SalesReceipt.objects.create(sales_invoice=self.invoice, amount=100)
        audited_update(SalesReceipt.objects.filter(pk=receipt.pk), amount=150)
        event = self.event('sales.salesreceipt', receipt.id, 'bulk_update')
        self.assertEqual(event.business_id, self.business.id)
        self.assertEqual(event.changes, {'amount': [100.0, 150.0]})

    def test_the_owner_now_sees_these(self):
        from sales.models import SalesReceipt
        receipt = SalesReceipt.objects.create(sales_invoice=self.invoice, amount=75)
        rows = client_for(self.owner).get('/audit-events/', {'model': 'sales.salesreceipt'}).json()
        self.assertIn(str(receipt.id), [row['object_id'] for row in rows])


class SharedListTests(AuditFixture):
    """A city, a unit: no business to find. They go to the business of
    whoever changed them - never to one reached through a link that fans
    out (a city's customers can be in any shop)."""

    def test_a_shared_record_goes_to_the_actors_business(self):
        other_owner = User.objects.create_user(email='other@example.com', password='pw')
        other = Business.objects.create(name='Other Shop', owner=other_owner, phone='03007654321',
                                        is_active=True)
        Customer.objects.create(business=other, city=self.city, name='Theirs')

        # The shared lists are read-only through the API (the admin and the
        # imports add to them), so a signed-in request is set up directly.
        # This city's only customer is in the other shop: renaming it is
        # still the event of the business whose owner did it.
        context.start_request()
        try:
            context.set_actor(self.owner)
            self.city.name = 'Sukkur City'
            self.city.save()
        finally:
            context.end_request()
        renamed = AuditEvent.objects.get(model='root.city', object_id=str(self.city.id), action='update')
        self.assertEqual(renamed.business_id, self.business.id)
        self.assertEqual(renamed.actor, self.owner)

    def test_with_nobody_acting_it_has_none(self):
        city = City.objects.create(name='Larkana', postal_code='77150')
        event = AuditEvent.objects.get(model='root.city', object_id=str(city.id), action='create')
        self.assertIsNone(event.business_id)


class ActorTests(AuditFixture):

    def test_an_admin_site_edit_names_the_admin(self):
        from django.test import Client
        admin = User.objects.create_superuser(email='site-admin@example.com', password='pw-for-tests')
        browser = Client()
        browser.force_login(admin)
        response = browser.post(f'/admin/root/city/{self.city.id}/change/',
                                {'name': 'Sukkur Division', 'postal_code': '65200'})
        self.assertEqual(response.status_code, 302, response.content[:500])
        event = AuditEvent.objects.get(model='root.city', object_id=str(self.city.id), action='update')
        self.assertEqual(event.actor, admin)

    def test_signing_up_names_the_new_user(self):
        response = APIClient().post('/auth/users/', {
            'email': 'newcomer@example.com', 'password': 'a-long-pass-123',
            're_password': 'a-long-pass-123'}, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        user = User.objects.get(email='newcomer@example.com')
        event = AuditEvent.objects.get(model='core.user', object_id=str(user.id), action='create')
        self.assertEqual(event.actor, user)

    def test_a_password_is_never_written_down(self):
        user = User.objects.create_user(email='secret@example.com', password='pw-secret-1')
        event = AuditEvent.objects.get(model='core.user', object_id=str(user.id), action='create')
        self.assertEqual(event.changes['password'], [None, '(hidden)'])

        user.set_password('pw-secret-2')
        user.save()
        changed = AuditEvent.objects.get(model='core.user', object_id=str(user.id), action='update')
        self.assertEqual(changed.changes, {'password': ['(hidden)', '(hidden)']})

    def test_old_events_holding_a_hash_are_masked_when_read(self):
        AuditEvent.objects.create(business=self.business, source='ui', model='core.user',
                                  object_id='1', action='create',
                                  changes={'password': [None, 'pbkdf2_sha256$x'], 'email': [None, 'x@y.z']})
        rows = client_for(self.owner).get('/audit-events/', {'model': 'core.user', 'object_id': '1'}).json()
        self.assertEqual(rows[0]['changes'], {'password': [None, '(hidden)'], 'email': [None, 'x@y.z']})
