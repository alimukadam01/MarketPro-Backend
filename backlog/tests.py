"""
The backlog data entry pipeline (plans/backlog-data-entry.md, M1-M7).

Requests carry a real JWT (audit/tests.py explains why), and uploaded files
go to a temporary MEDIA_ROOT.
"""
import io
import shutil
import tempfile
import zipfile
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from inventory.models import InventoryItem
from root.models import (
    Business, BusinessConfig, City, Customer, Employee, EmployeeAccess, Location, Product,
    ProductVariant, Supplier, Unit,
)
from sales.models import InvoiceImage, PurchaseInvoice, ReturnedItem, SalesInvoice, SalesInvoiceItem
from .metrics import person_metrics
from .models import (
    BacklogEntry, BacklogNewRecord, BacklogOutput, BacklogReview, GlossaryEntry, GlossarySuggestion,
    UploadBatch,
)

User = get_user_model()
MEDIA = tempfile.mkdtemp(prefix='backlog-tests-')


def png(color='white'):
    buffer = io.BytesIO()
    Image.new('RGB', (8, 8), color).save(buffer, 'PNG')
    return buffer.getvalue()


def upload(name, data):
    from django.core.files.uploadedfile import SimpleUploadedFile
    return SimpleUploadedFile(name, data)


def client_for(user):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'JWT {AccessToken.for_user(user)}')
    return client


@override_settings(MEDIA_ROOT=MEDIA)
class BacklogFixture(TestCase):

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(email='bl-owner@example.com', password='pw-for-tests')
        cls.business = Business.objects.create(name='Backlog Paints', owner=cls.owner,
                                               phone='03001234567', is_active=True)
        BusinessConfig.objects.create(business=cls.business, accounting=True)
        cls.city = City.objects.create(name='Sukkur', postal_code='65200')
        cls.customer = Customer.objects.create(business=cls.business, name='Hassan Builders', city=cls.city)
        cls.supplier = Supplier.objects.create(business=cls.business, name='Jotun')
        cls.unit = Unit.objects.create(name='Tin', abv='Tin')
        cls.shop = Location.objects.create(business=cls.business, name='Shop', address='-', is_default=True)
        cls.paint = Product.objects.create(business=cls.business, name='Weather Emulsion', unit=cls.unit)
        cls.white = ProductVariant.objects.create(base=cls.paint, name='Sweet White', sku='BL-1')
        cls.stock = InventoryItem.objects.create(
            business=cls.business, inventory=cls.business.inventory_glance, product=cls.white,
            location=cls.shop, quantity=50, quantity_on_hand=50, unit_price=2150, unit_cost=1800)
        cls.typist = cls._entry_user('typist@example.com')
        cls.second = cls._entry_user('second@example.com')

    @classmethod
    def _entry_user(cls, email):
        user = User.objects.create_user(email=email, password='pw-for-tests', role=User.BACKLOG)
        employee = Employee.objects.create(user=user, business=cls.business, created_by=cls.owner)
        EmployeeAccess.objects.create(employee=employee, permissions={})
        return user

    def entry(self, type='sales_invoice', **extra):
        from django.core.files.base import ContentFile
        return BacklogEntry.objects.create(
            business=self.business, type=type, created_by=self.owner,
            image=ContentFile(png(), name='page.png'), **extra)

    def claimed(self, user=None, **extra):
        entry = self.entry(**extra)
        response = client_for(user or self.typist).post(f'/backlog-work/{entry.id}/claim/')
        self.assertEqual(response.status_code, 200, response.content)
        entry.refresh_from_db()
        return entry

    def sale_payload(self, quantity=4, price=2150, number='1427'):
        return {'invoice_number': number, 'customer': self.customer.id,
                'date_issued': timezone.now().isoformat(),
                'items': [{'product_id': self.white.id, 'quantity': quantity, 'unit_price': price}]}


class UploadTests(BacklogFixture):

    def post(self, files, **fields):
        data = {'type': 'sales_invoice', 'book_label': 'Sales 2019 Vol 3', 'files': files, **fields}
        return client_for(self.owner).post('/backlog-batches/', data, format='multipart')

    def test_photos_each_become_a_pending_page(self):
        response = self.post([upload('a.png', png()), upload('b.png', png('red'))],
                             is_daily='true', priority='high', notes='Pages 40-52 are returns')
        self.assertEqual(response.status_code, 201, response.content)
        batch = UploadBatch.objects.get()
        entries = BacklogEntry.objects.filter(batch=batch)
        self.assertEqual(entries.count(), 2)
        self.assertTrue(all(e.status == 'pending' and e.is_daily and e.priority == 'high'
                            and e.book_label == 'Sales 2019 Vol 3' for e in entries))

    def test_a_zip_gives_its_images_only(self):
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, 'w') as nested:
            nested.writestr('x.png', png())
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as zipped:
            zipped.writestr('Book/p1.png', png())
            zipped.writestr('../../escape.png', png('blue'))
            zipped.writestr('notes.pdf', b'%PDF-1.4 not an image')
            zipped.writestr('inner.zip', inner.getvalue())
            zipped.writestr('Book/', b'')
        response = self.post([upload('book.zip', archive.getvalue())])
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()['pages'], 2)
        self.assertEqual(response.json()['skipped'], 2)
        names = sorted(e.image.name.split('/')[-1].split('_')[0].split('.')[0]
                       for e in BacklogEntry.objects.all())
        self.assertEqual(names, ['escape', 'p1'])
        # The zip is unpacked, never stored.
        self.assertFalse(any(e.image.name.endswith('.zip') for e in BacklogEntry.objects.all()))

    def test_nothing_usable_is_refused(self):
        response = self.post([upload('a.txt', b'hello')])
        self.assertEqual(response.status_code, 400)
        self.assertFalse(BacklogEntry.objects.exists())

    def test_an_oversized_zip_is_refused(self):
        from backlog import uploads
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as zipped:
            for n in range(5):
                zipped.writestr(f'p{n}.png', png())
        original = uploads.MAX_ZIP_ENTRIES
        uploads.MAX_ZIP_ENTRIES = 3
        try:
            response = self.post([upload('big.zip', archive.getvalue())])
        finally:
            uploads.MAX_ZIP_ENTRIES = original
        self.assertEqual(response.status_code, 400)

    def test_data_entry_cannot_upload(self):
        response = client_for(self.typist).post('/backlog-batches/', {'type': 'sales_invoice'})
        self.assertEqual(response.status_code, 403)


class QueueAndClaimTests(BacklogFixture):

    def test_one_page_one_person(self):
        entry = self.entry()
        first = client_for(self.typist).post(f'/backlog-work/{entry.id}/claim/')
        second = client_for(self.second).post(f'/backlog-work/{entry.id}/claim/')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
        entry.refresh_from_db()
        self.assertEqual((entry.status, entry.claimed_by), ('in_progress', self.typist))

    def test_one_open_page_at_a_time(self):
        self.claimed()
        other = self.entry()
        self.assertEqual(client_for(self.typist).post(f'/backlog-work/{other.id}/claim/').status_code, 409)

    def test_the_queue_order(self):
        old = self.entry()
        daily = self.entry(is_daily=True)
        sent_back = self.entry(rejection_count=1, review_note='Line 2 is 12')
        high = self.entry(priority='high')
        mine = self.claimed()
        items = client_for(self.typist).get('/backlog-work/queue/').json()['items']
        self.assertEqual([row['id'] for row in items], [mine.id, sent_back.id, daily.id, high.id, old.id])
        self.assertTrue(items[0]['mine'])

    def test_someone_elses_page_is_closed(self):
        entry = self.claimed(user=self.second)
        client = client_for(self.typist)
        self.assertEqual(client.get(f'/backlog-work/{entry.id}/').status_code, 403)
        self.assertEqual(client.post(f'/backlog-work/{entry.id}/sales-invoice/', self.sale_payload(),
                                     format='json').status_code, 403)

    def test_release_and_cant_read(self):
        entry = self.claimed()
        client = client_for(self.typist)
        self.assertEqual(client.post(f'/backlog-work/{entry.id}/release/').status_code, 200)
        entry.refresh_from_db()
        self.assertEqual((entry.status, entry.claimed_by), ('pending', None))
        entry = self.claimed()
        self.assertEqual(client.post(f'/backlog-work/{entry.id}/cant-read/', {}).status_code, 400)
        client.post(f'/backlog-work/{entry.id}/cant-read/', {'note': 'Bottom half cut off'}, format='json')
        entry.refresh_from_db()
        self.assertEqual((entry.status, entry.cant_read_note), ('cant_read', 'Bottom half cut off'))


class VisibilityTests(BacklogFixture):

    FIGURES = {'quantity_on_hand', 'unit_cost', 'unit_price', 'balance', 'stock', 'total_sales'}

    def test_lookups_carry_names_only(self):
        client = client_for(self.typist)
        for kind in ('variants', 'products', 'customers', 'suppliers', 'accounts', 'units',
                     'locations', 'attributes', 'projects'):
            with self.subTest(kind):
                response = client.get(f'/backlog-work/lookup/{kind}/', {'q': ''})
                self.assertEqual(response.status_code, 200)
                for row in response.json():
                    self.assertFalse(set(row) & self.FIGURES, row)
                    # product / product_name: a variant's product, so a name typed
                    # can be matched to it for "Add variants" - an id and a name.
                    self.assertLessEqual(set(row), {'id', 'label', 'unit', 'type', 'is_default',
                                                    'product', 'product_name'})
        # On a sales page, whether a product is in stock - yes or no, no figure.
        stocked = client.get('/backlog-work/lookup/variants/', {'q': 'emulsion', 'stock': '1'}).json()
        self.assertEqual([(row['id'], row['in_stock']) for row in stocked], [(self.white.id, True)])
        InventoryItem.objects.filter(pk=self.stock.pk).update(quantity_on_hand=0)
        stocked = client.get('/backlog-work/lookup/variants/', {'q': 'emulsion', 'stock': '1'}).json()
        self.assertEqual(stocked[0]['in_stock'], False)
        self.assertFalse(set(stocked[0]) & self.FIGURES)
        found = client.get('/backlog-work/lookup/variants/', {'q': 'emulsion white'}).json()
        self.assertEqual([row['id'] for row in found], [self.white.id])

    def test_an_entered_invoice_answers_with_the_bill_only(self):
        entry = self.claimed()
        response = client_for(self.typist).post(f'/backlog-work/{entry.id}/sales-invoice/',
                                                self.sale_payload(), format='json')
        self.assertEqual(response.status_code, 201, response.content)
        output = response.json()['output']
        self.assertEqual(output['kind'], 'sales_invoice')
        self.assertEqual(output['detail'], 'Hassan Builders')
        self.assertFalse(set(output) & self.FIGURES)

    def test_the_owner_pages_are_closed_to_data_entry(self):
        client = client_for(self.typist)
        for url in ('/backlog-entries/', '/backlog-batches/', '/backlog-glossary/',
                    '/backlog-entries/metrics/'):
            with self.subTest(url):
                self.assertEqual(client.get(url).status_code, 403)

    def test_a_page_only_takes_records_of_its_type(self):
        entry = self.claimed(type='product_list')
        response = client_for(self.typist).post(f'/backlog-work/{entry.id}/sales-invoice/',
                                                self.sale_payload(), format='json')
        self.assertEqual(response.status_code, 400)


class EntryAndReviewTests(BacklogFixture):

    def enter_sale(self, user=None):
        entry = self.claimed(user=user)
        client = client_for(user or self.typist)
        output = client.post(f'/backlog-work/{entry.id}/sales-invoice/', self.sale_payload(),
                             format='json').json()['output']
        return entry, output, client

    def test_enter_pay_submit_approve(self):
        entry, output, client = self.enter_sale()
        stock_after_sale = InventoryItem.objects.get(pk=self.stock.pk).quantity_on_hand
        self.assertEqual(stock_after_sale, 46)

        paid = client.post(f'/backlog-work/{entry.id}/outputs/{output["id"]}/payments/',
                           {'amount': 5000}, format='json')
        self.assertEqual(paid.status_code, 201, paid.content)

        self.assertEqual(client.post(f'/backlog-work/{entry.id}/submit/', {'seconds_spent': 372},
                                     format='json').status_code, 200)
        entry.refresh_from_db()
        self.assertEqual((entry.status, entry.entered_by, entry.seconds_spent), ('entered', self.typist, 372))

        owner = client_for(self.owner)
        queue = owner.get('/backlog-entries/review-queue/').json()
        self.assertEqual([row['id'] for row in queue], [entry.id])
        image_name = entry.image.name
        self.assertEqual(owner.post(f'/backlog-entries/{entry.id}/approve/').status_code, 200)
        entry.refresh_from_db()
        self.assertEqual(entry.status, 'verified')
        self.assertFalse(entry.image)
        # The photo moves onto the invoice (§9).
        attached = InvoiceImage.objects.get(sales_invoice_id=output['record_id'])
        self.assertEqual(attached.image.name, image_name)
        self.assertEqual(BacklogReview.objects.get(entry=entry).outcome, 'approved')

    def test_a_payment_on_a_page_sent_back(self):
        from accounts.models import MoneyAccount
        cash = MoneyAccount.objects.create(business=self.business, name='Counter', type='cash', is_default=True)
        entry, output, client = self.enter_sale()
        client.post(f'/backlog-work/{entry.id}/submit/')
        client_for(self.owner).post(f'/backlog-entries/{entry.id}/send-back/', {'reason': 'Add the cash'},
                                    format='json')
        again = client_for(self.second)
        self.assertEqual(again.post(f'/backlog-work/{entry.id}/claim/').status_code, 200)
        # Exactly what the payments dialog sends.
        body = {'amount': 3000, 'desc': '', 'account': cash.id, 'payment_method': 'cash',
                'date': timezone.localdate().isoformat()}
        paid = again.post(f'/backlog-work/{entry.id}/outputs/{output["id"]}/payments/', body, format='json')
        self.assertEqual(paid.status_code, 201, paid.content)

    def test_what_the_khaata_settles_without_the_balance(self):
        """BUGS.md #004: the entry person's dialog learns how much of this
        invoice the khaata covers - and so what one more payment may be -
        but never the customer's balance or the credits behind it."""
        import datetime
        from accounts.models import MoneyAccount, PartyPayment, Transaction
        cash = MoneyAccount.objects.create(business=self.business, name='Counter', type='cash', is_default=True)
        # PKR 10,000 the customer paid on account, before this bill.
        money = Transaction.objects.create(
            business=self.business, account=cash, type='customer_receipt', amount=10000,
            date=datetime.date(2019, 1, 1), status='C', payment_method='cash')
        PartyPayment.objects.create(business=self.business, transaction=money, customer=self.customer)

        entry, output, client = self.enter_sale()     # 4 x 2,150 = 8,600
        url = f'/backlog-work/{entry.id}/outputs/{output["id"]}/'
        settled = client.get(url + 'settlement/')
        self.assertEqual(settled.status_code, 200, settled.content)
        data = settled.json()
        self.assertEqual((data['total'], data['settled_from_khaata'], data['room'], data['still_owed']),
                         (8600, 8600, 0, 0))
        # One line for this invoice: no credit's remaining amount, date or reference.
        self.assertEqual(data['settled_by'], [{'kind': 'khaata', 'id': 0,
                                               'label': "Already on the customer's khaata",
                                               'reference': '', 'date': None, 'applied': 8600}])
        self.assertNotIn('overpaid', data)
        self.assertNotIn('1400', settled.content.decode())     # what is left on account

        body = {'amount': 100, 'account': cash.id, 'payment_method': 'cash',
                'date': timezone.localdate().isoformat()}
        self.assertEqual(client.post(url + 'payments/', body, format='json').status_code, 400)

    def test_saving_a_page_keeps_the_bills_date(self):
        """The date is read back as the shop's day, so saving it again - as
        every correction and every approval does - leaves it alone."""
        entry = self.claimed()
        client = client_for(self.typist)
        payload = {**self.sale_payload(), 'date_issued': '2025-05-05', 'date_due': '2025-05-05'}
        output = client.post(f'/backlog-work/{entry.id}/sales-invoice/', payload, format='json').json()['output']
        url = f'/backlog-work/{entry.id}/outputs/{output["id"]}/'
        values = client.get(url).json()['values']
        self.assertEqual(values['date_issued'], '2025-05-05')

        line = values['lines'][0]
        again = {**payload, 'date_issued': values['date_issued'],
                 'items': [{'id': line['id'], 'product_id': self.white.id, 'quantity': 4, 'unit_price': 2150}]}
        for _ in range(2):
            self.assertEqual(client.put(url, again, format='json').status_code, 200)
        self.assertEqual(client.get(url).json()['values']['date_issued'], '2025-05-05')

    def test_submit_needs_something_entered(self):
        entry = self.claimed()
        self.assertEqual(client_for(self.typist).post(f'/backlog-work/{entry.id}/submit/').status_code, 400)

    def test_send_back_keeps_the_records_and_goes_to_the_top(self):
        entry, output, client = self.enter_sale()
        client.post(f'/backlog-work/{entry.id}/submit/')
        owner = client_for(self.owner)
        self.assertEqual(owner.post(f'/backlog-entries/{entry.id}/send-back/', {}).status_code, 400)
        response = owner.post(f'/backlog-entries/{entry.id}/send-back/', {'reason': 'Line 1 is 12, not 4'},
                              format='json')
        self.assertEqual(response.status_code, 200)
        entry.refresh_from_db()
        self.assertEqual((entry.status, entry.rejection_count, entry.review_note),
                         ('pending', 1, 'Line 1 is 12, not 4'))
        self.assertTrue(BacklogOutput.objects.filter(entry=entry).exists())
        self.entry()  # an older page in the pool does not jump ahead of it
        first = client_for(self.second).get('/backlog-work/queue/').json()['items'][0]
        self.assertEqual(first['id'], entry.id)

    def test_accuracy_is_approved_over_reviewed(self):
        for outcome in ('approve', 'send-back', 'approve'):
            entry, _, client = self.enter_sale()
            client.post(f'/backlog-work/{entry.id}/submit/')
            client_for(self.owner).post(f'/backlog-entries/{entry.id}/{outcome}/', {'reason': 'x'},
                                        format='json')
        mine = [row for row in person_metrics(self.business) if row['user']['id'] == self.typist.id][0]
        self.assertEqual((mine['approved'], mine['reviewed'], mine['accuracy']), (2, 3, 66.7))

    def test_only_the_owner_reviews(self):
        entry, _, client = self.enter_sale()
        client.post(f'/backlog-work/{entry.id}/submit/')
        staff = User.objects.create_user(email='staff@example.com', password='pw', role=User.EMPLOYEE)
        employee = Employee.objects.create(user=staff, business=self.business, created_by=self.owner)
        EmployeeAccess.objects.create(employee=employee, permissions={'backlog_entries': {
            'view': True, 'create': True, 'edit': True, 'delete': True}})
        self.assertEqual(client_for(staff).post(f'/backlog-entries/{entry.id}/approve/').status_code, 403)

    def test_the_owner_corrects_an_entered_invoice_in_place(self):
        entry, output, client = self.enter_sale()
        client.post(f'/backlog-work/{entry.id}/outputs/{output["id"]}/payments/', {'amount': 1000},
                    format='json')
        client.post(f'/backlog-work/{entry.id}/submit/')
        owner = client_for(self.owner)

        # The page under review opens on the entry screen, with everything typed.
        self.assertEqual(owner.get(f'/backlog-work/{entry.id}/').status_code, 200)
        values = owner.get(f'/backlog-work/{entry.id}/outputs/{output["id"]}/').json()['values']
        self.assertEqual(values['party'], {'id': self.customer.id, 'label': 'Hassan Builders'})
        self.assertEqual([(l['quantity'], l['price']) for l in values['lines']], [(4, 2150)])
        self.assertEqual([p['amount'] for p in values['payments']], [1000])

        # Corrected through the invoice update: the stock follows.
        line = values['lines'][0]
        payload = {**self.sale_payload(), 'items': [{'id': line['id'], 'product_id': self.white.id,
                                                     'quantity': 12, 'unit_price': 2150}]}
        response = owner.put(f'/backlog-work/{entry.id}/outputs/{output["id"]}/', payload, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(InventoryItem.objects.get(pk=self.stock.pk).quantity_on_hand, 38)

        payment = values['payments'][0]['id']
        self.assertEqual(owner.delete(
            f'/backlog-work/{entry.id}/outputs/{output["id"]}/payments/{payment}/').status_code, 204)
        self.assertFalse(SalesInvoice.objects.get(pk=output['record_id']).payment_receipts.exists())

        # A line corrected downwards is replaced: its 12 come back, 2 go out.
        payload = {**self.sale_payload(), 'items': [{'id': None, 'product_id': self.white.id, 'quantity': 2,
                                                     'unit_price': 2150}]}
        response = owner.put(f'/backlog-work/{entry.id}/outputs/{output["id"]}/', payload, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(InventoryItem.objects.get(pk=self.stock.pk).quantity_on_hand, 48)

        # Review opens records, not the page itself: submitting stays with entry.
        self.assertEqual(owner.post(f'/backlog-work/{entry.id}/submit/').status_code, 403)
        # And the entry person has handed it in, so it is closed to them.
        self.assertEqual(client.get(f'/backlog-work/{entry.id}/').status_code, 403)


class DeletingOutputsTests(BacklogFixture):

    def test_deleting_a_sales_invoice_returns_its_stock(self):
        entry = self.claimed()
        client = client_for(self.typist)
        output = client.post(f'/backlog-work/{entry.id}/sales-invoice/', self.sale_payload(quantity=7),
                             format='json').json()['output']
        self.assertEqual(InventoryItem.objects.get(pk=self.stock.pk).quantity_on_hand, 43)
        self.assertEqual(client.delete(f'/backlog-work/{entry.id}/outputs/{output["id"]}/').status_code, 204)
        self.assertEqual(InventoryItem.objects.get(pk=self.stock.pk).quantity_on_hand, 50)
        self.assertFalse(SalesInvoice.objects.filter(pk=output['record_id']).exists())

    def test_deleting_a_purchase_invoice_takes_its_stock_back(self):
        entry = self.claimed(type='purchase_invoice')
        client = client_for(self.typist)
        payload = {'invoice_number': 'P-1', 'supplier': self.supplier.id, 'status': 'R',
                   'date_issued': timezone.now().isoformat(),
                   'items': [{'product_id': self.white.id, 'quantity': 10, 'unit_cost': 1800}]}
        response = client.post(f'/backlog-work/{entry.id}/purchase-invoice/', payload, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        output = response.json()['output']
        self.assertEqual(InventoryItem.objects.get(pk=self.stock.pk).quantity_on_hand, 60)
        client.delete(f'/backlog-work/{entry.id}/outputs/{output["id"]}/')
        self.assertEqual(InventoryItem.objects.get(pk=self.stock.pk).quantity_on_hand, 50)
        self.assertFalse(PurchaseInvoice.objects.filter(pk=output['record_id']).exists())

    def test_a_purchase_entered_and_corrected_as_the_screen_sends_it(self):
        entry = self.claimed(type='purchase_invoice')
        client = client_for(self.typist)
        day = timezone.localdate().isoformat()
        payload = {'invoice_number': 'P-2', 'supplier': self.supplier.id, 'status': 'R',
                   'date_issued': day, 'date_due': day, 'delivery': None, 'notes': '',
                   'tax': {'value': 0, 'type': 'percentage'}, 'project': None,
                   'items': [{'id': None, 'product_id': self.white.id, 'quantity': 10, 'unit_cost': 1800}]}
        response = client.post(f'/backlog-work/{entry.id}/purchase-invoice/', payload, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        output = response.json()['output']
        values = client.get(f'/backlog-work/{entry.id}/outputs/{output["id"]}/').json()['values']
        self.assertEqual((values['status'], values['party']['id']), ('R', self.supplier.id))

        line = values['lines'][0]
        payload['items'] = [{'id': line['id'], 'product_id': self.white.id, 'quantity': 6, 'unit_cost': 1800}]
        response = client.put(f'/backlog-work/{entry.id}/outputs/{output["id"]}/', payload, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(InventoryItem.objects.get(pk=self.stock.pk).quantity_on_hand, 56)

    def test_a_stock_line_on_an_existing_row_is_put_back(self):
        entry = self.claimed(type='stock_list')
        client = client_for(self.typist)
        response = client.post(f'/backlog-work/{entry.id}/inventory/',
                               {'product': self.white.id, 'quantity': 42, 'counted_at': '2026-10-01'},
                               format='json')
        self.assertEqual(response.status_code, 201, response.content)
        output = response.json()['output']
        self.assertFalse(output['created'])
        row = InventoryItem.objects.get(pk=self.stock.pk)
        self.assertEqual((row.quantity_on_hand, str(row.stock_counted_at)), (42, '2026-10-01'))
        # The response never shows the stock that was there before.
        self.assertNotIn('50', str(output))
        client.delete(f'/backlog-work/{entry.id}/outputs/{output["id"]}/')
        row.refresh_from_db()
        self.assertEqual((row.quantity_on_hand, row.stock_counted_at), (50, None))

    def test_a_price_list_sets_prices_only(self):
        entry = self.claimed(type='price_list')
        client = client_for(self.typist)
        client.post(f'/backlog-work/{entry.id}/inventory/',
                    {'product': self.white.id, 'unit_price': 2300, 'quantity': 999}, format='json')
        row = InventoryItem.objects.get(pk=self.stock.pk)
        self.assertEqual((row.unit_price, row.quantity_on_hand), (2300, 50))

    def test_a_product_list_creates_and_edits_products(self):
        entry = self.claimed(type='product_list')
        client = client_for(self.typist)
        response = client.post(f'/backlog-work/{entry.id}/products/', {
            'name': 'Synthetic Enamel', 'unit': self.unit.id, 'desc': 'Gloss',
            'variants': [{'attributes': {'Shade': 'Black', 'Size': 'Quarter'}},
                         {'attributes': {'Shade': 'White', 'Size': 'Quarter'}}]}, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        output = response.json()['output']
        self.assertEqual((output['label'], output['variant_count']), ('Synthetic Enamel', 2))
        values = client.get(f'/backlog-work/{entry.id}/outputs/{output["id"]}/').json()['values']
        edited = client.put(f'/backlog-work/{entry.id}/outputs/{output["id"]}/', {
            'name': 'Synthetic Enamel', 'unit': self.unit.id, 'desc': 'Gloss',
            'variants': values['variants'][:1]}, format='json')
        self.assertEqual(edited.status_code, 200, edited.content)
        self.assertEqual(Product.objects.get(pk=output['record_id']).variants.count(), 1)
        self.assertEqual(client.delete(f'/backlog-work/{entry.id}/outputs/{output["id"]}/').status_code, 204)
        self.assertFalse(Product.objects.filter(pk=output['record_id']).exists())


class NewRecordsTests(BacklogFixture):
    """BUGS.md #006: a customer, supplier or product the page names but
    MarketPro lacks is created from the entry screen."""

    def test_a_new_customer_with_what_they_owe(self):
        from accounts.models import PartyOpeningBalance
        entry = self.claimed()
        client = client_for(self.typist)
        url = f'/backlog-work/{entry.id}/new-customer/'
        self.assertEqual(client.post(url, {'name': 'Shafiq Ahmed', 'city': 99999}, format='json').status_code, 400)
        response = client.post(url, {
            'name': ' Shafiq Ahmed ', 'phone': '0300 1112223', 'city': self.city.id,
            'opening_balance': {'amount': 5000, 'as_of_date': '2019-01-01'}}, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        customer = Customer.objects.get(pk=response.json()['id'])
        self.assertEqual(response.json(), {'id': customer.id, 'label': 'Shafiq Ahmed'})
        self.assertEqual((customer.business, customer.city), (self.business, self.city))
        balance = PartyOpeningBalance.objects.get(customer=customer)
        self.assertEqual((balance.amount, str(balance.as_of_date)), (5000, '2019-01-01'))

    def test_a_new_supplier_without_a_balance(self):
        from accounts.models import PartyOpeningBalance
        entry = self.claimed(type='purchase_invoice')
        response = client_for(self.typist).post(f'/backlog-work/{entry.id}/new-supplier/', {
            'name': 'Nippon', 'business_name': 'Nippon Paint Pakistan',
            'opening_balance': {'amount': 0, 'as_of_date': '2019-01-01'}}, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()['label'], 'Nippon (Nippon Paint Pakistan)')
        self.assertFalse(PartyOpeningBalance.objects.filter(supplier_id=response.json()['id']).exists())

    def test_a_new_product_comes_with_its_stock_rows(self):
        entry = self.claimed()
        client = client_for(self.typist)
        url = f'/backlog-work/{entry.id}/new-product/'
        body = {'name': 'Enamel', 'unit': self.unit.id, 'variants': [
            {'attributes': {'Shade': 'Black'}}, {'attributes': {'Shade': 'White'}}],
            'stock': [{'quantity': 12, 'unit_price': 680, 'unit_cost': 520}]}
        self.assertEqual(client.post(url, body, format='json').status_code, 400)   # one row short

        body['stock'].append({})
        response = client.post(url, body, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        variants = response.json()['variants']
        self.assertEqual([v['label'] for v in variants], ['Enamel (Black)', 'Enamel (White)'])
        black, white = (InventoryItem.objects.get(product_id=v['id']) for v in variants)
        self.assertEqual((black.quantity_on_hand, black.unit_price, black.stock_counted_at, black.location),
                         (12, 680, timezone.localdate(), self.shop))
        self.assertEqual((white.quantity_on_hand, white.unit_price, white.stock_counted_at, white.location),
                         (0, None, None, self.shop))

        # And it can go straight onto the bill.
        sale = client.post(f'/backlog-work/{entry.id}/sales-invoice/', {
            'invoice_number': 'N-1', 'customer': self.customer.id, 'date_issued': timezone.localdate().isoformat(),
            'items': [{'product_id': variants[1]['id'], 'quantity': 1, 'unit_price': 700}]}, format='json')
        self.assertEqual(sale.status_code, 201, sale.content)

    def test_the_owner_sees_what_was_added_from_the_page(self):
        entry = self.claimed()
        client = client_for(self.typist)
        customer = client.post(f'/backlog-work/{entry.id}/new-customer/', {
            'name': 'Shafiq Ahmed', 'city': self.city.id,
            'opening_balance': {'amount': 5000, 'as_of_date': '2019-01-01'}}, format='json').json()
        product = client.post(f'/backlog-work/{entry.id}/new-product/', {
            'name': 'Enamel', 'unit': self.unit.id, 'stock': [{}]}, format='json').json()

        rows = client_for(self.owner).get(f'/backlog-entries/{entry.id}/').json()['new_records']
        self.assertEqual([(r['kind'], r['record_id'], r['label'], r['detail'], r['opening_balance']) for r in rows], [
            ('customer', customer['id'], 'Shafiq Ahmed', 'Sukkur', 5000),
            ('product', product['product']['id'], 'Enamel', '1 variant · Tin', None),
        ])
        # The entry person's view of the page carries no balance.
        self.assertNotIn('new_records', client.get(f'/backlog-work/{entry.id}/').json())

    def test_the_invoice_screens_create_the_same_way(self):
        """The create screens' endpoints make the same records, without a
        page - and are closed to a data entry account."""
        from accounts.models import PartyOpeningBalance
        owner = client_for(self.owner)
        customer = owner.post('/customers/create-with-balance/', {
            'name': 'Walk-in Builders', 'city': self.city.id,
            'opening_balance': {'amount': 750, 'as_of_date': '2026-01-01'}}, format='json')
        self.assertEqual(customer.status_code, 201, customer.content)
        self.assertEqual(PartyOpeningBalance.objects.get(customer_id=customer.json()['id']).amount, 750)

        supplier = owner.post('/suppliers/create-with-balance/', {'name': 'Nippon'}, format='json')
        self.assertEqual((supplier.status_code, supplier.json()['label']), (201, 'Nippon'))

        product = owner.post('/products/create-with-stock/', {
            'name': 'Gloss', 'unit': self.unit.id, 'stock': [{'quantity': 5, 'unit_price': 900}]}, format='json')
        self.assertEqual(product.status_code, 201, product.content)
        variant = product.json()['variants'][0]['id']
        self.assertEqual(InventoryItem.objects.get(product_id=variant).quantity_on_hand, 5)
        self.assertFalse(BacklogNewRecord.objects.exists())

        typist = client_for(self.typist)
        for url in ('/customers/create-with-balance/', '/suppliers/create-with-balance/',
                    '/products/create-with-stock/'):
            self.assertEqual(typist.post(url, {'name': 'X'}, format='json').status_code, 403, url)

    def test_variants_added_to_a_product_the_name_matched(self):
        owner = client_for(self.owner)
        url = f'/products/{self.paint.id}/variants-and-stock/'
        shown = owner.get(url).json()
        self.assertEqual((shown['name'], shown['unit']['label']), ('Weather Emulsion', 'Tin'))
        self.assertEqual([v['id'] for v in shown['variants']], [self.white.id])

        response = owner.post(url, {
            'name': 'Weather Emulsion', 'unit': self.unit.id, 'desc': 'Exterior',
            'variants': [{'id': self.white.id, 'attributes': {'Shade': 'Brilliant White'}},
                         {'attributes': {'Shade': 'Sky Blue'}}],
            'stock': [{'quantity': 7, 'unit_price': 2300}]}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        new = response.json()['variants']
        self.assertEqual([v['label'] for v in new], ['Weather Emulsion (Sky Blue)'])
        self.assertEqual(InventoryItem.objects.get(product_id=new[0]['id']).quantity_on_hand, 7)
        self.white.refresh_from_db()
        self.assertEqual(self.white.name, 'Brilliant White')
        self.paint.refresh_from_db()
        self.assertEqual(self.paint.desc, 'Exterior')

        # A variant left off the list is never deleted here.
        owner.post(url, {'name': 'Weather Emulsion', 'unit': self.unit.id, 'variants': []}, format='json')
        self.assertTrue(ProductVariant.objects.filter(pk=self.white.pk).exists())

        # Another product's variant is refused.
        other = Product.objects.create(business=self.business, name='Putty', unit=self.unit)
        stray = ProductVariant.objects.create(base=other, name='default', sku='BL-9')
        bad = owner.post(url, {'name': 'Weather Emulsion', 'unit': self.unit.id,
                               'variants': [{'id': stray.id, 'attributes': {}}]}, format='json')
        self.assertEqual(bad.status_code, 400)

    def test_the_entry_person_adds_variants_from_the_page(self):
        entry = self.claimed()
        client = client_for(self.typist)
        found = client.get('/backlog-work/lookup/variants/?q=Weather').json()
        self.assertEqual((found[0]['product'], found[0]['product_name']), (self.paint.id, 'Weather Emulsion'))
        url = f'/backlog-work/{entry.id}/products/{self.paint.id}/'
        self.assertEqual(client.get(url).json()['variants'][0]['id'], self.white.id)
        response = client.post(url, {
            'name': 'Weather Emulsion', 'unit': self.unit.id,
            'variants': [{'id': self.white.id, 'attributes': {}}, {'attributes': {'Shade': 'Red'}}],
            'stock': [{}]}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['variants'][0]['label'], 'Weather Emulsion (Red)')
        # Only with a page open, and never the shop's stock in the answer.
        self.assertNotIn('quantity', str(client.get(url).json()))
        self.assertEqual(client_for(self.second).get(url).status_code, 403)

    def test_staff_can_read_the_names_lookup(self):
        staff = User.objects.create_user(email='staff-lookup@example.com', password='pw', role=User.EMPLOYEE)
        Employee.objects.create(user=staff, business=self.business, created_by=self.owner)
        response = client_for(staff).get('/backlog-work/lookup/units/')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn('Tin', [row['label'] for row in response.json()])
        # The rest of the entry screens stays closed to them.
        self.assertEqual(client_for(staff).get('/backlog-work/queue/').status_code, 403)

    def test_only_with_a_page_open(self):
        entry = self.entry()
        response = client_for(self.typist).post(f'/backlog-work/{entry.id}/new-customer/', {
            'name': 'X', 'city': self.city.id}, format='json')
        self.assertEqual(response.status_code, 403)


class GlossaryTests(BacklogFixture):

    def test_versions_are_exact(self):
        owner = client_for(self.owner)
        first = owner.post('/backlog-glossary/', {'term': 'S.W', 'meaning': 'Sweet White', 'kind': 'shade'},
                           format='json').json()
        owner.post('/backlog-glossary/', {'term': 'Gln', 'meaning': 'Gallon', 'kind': 'unit'}, format='json')
        owner.patch(f'/backlog-glossary/{first["id"]}/', {'meaning': 'Sweet White (shade)'}, format='json')
        listing = owner.get('/backlog-glossary/').json()
        self.assertEqual(listing['version'], 3)
        self.assertEqual(sorted(e['meaning'] for e in listing['entries']), ['Gallon', 'Sweet White (shade)'])
        at = lambda v: sorted(GlossaryEntry.objects.filter(business=self.business).at_version(v)
                              .values_list('meaning', flat=True))
        self.assertEqual(at(1), ['Sweet White'])
        self.assertEqual(at(2), ['Gallon', 'Sweet White'])
        self.assertEqual(at(3), ['Gallon', 'Sweet White (shade)'])
        gln = [e for e in listing['entries'] if e['term'] == 'Gln'][0]
        owner.delete(f'/backlog-glossary/{gln["id"]}/')
        self.assertEqual(at(4), ['Sweet White (shade)'])
        self.assertEqual(at(3), ['Gallon', 'Sweet White (shade)'])

    def test_a_suggestion_becomes_a_term_with_its_page(self):
        entry = self.claimed()
        typist = client_for(self.typist)
        self.assertEqual(typist.get('/backlog-work/glossary/').status_code, 200)
        response = typist.post('/backlog-work/glossary-suggestions/', {
            'term': 'Bhai jan', 'meaning': 'Hassan Builders', 'kind': 'customer',
            'customer': self.customer.id, 'entry': entry.id}, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        owner = client_for(self.owner)
        pending = owner.get('/backlog-glossary-suggestions/').json()
        self.assertEqual(pending[0]['links_to']['name'], 'Hassan Builders')
        owner.post(f'/backlog-glossary-suggestions/{pending[0]["id"]}/approve/')
        row = GlossaryEntry.objects.get(term='Bhai jan')
        self.assertEqual((row.source_entry, row.valid_from, row.customer), (entry, 1, self.customer))
        self.assertEqual(GlossarySuggestion.objects.get().status, 'approved')

    def test_submit_records_the_glossary_version(self):
        client_for(self.owner).post('/backlog-glossary/', {'term': 'a', 'meaning': 'b'}, format='json')
        entry = self.claimed()
        client = client_for(self.typist)
        client.post(f'/backlog-work/{entry.id}/sales-invoice/', self.sale_payload(), format='json')
        client.post(f'/backlog-work/{entry.id}/submit/')
        entry.refresh_from_db()
        self.assertEqual(entry.glossary_version, 1)


class StockCountTests(BacklogFixture):
    """§8: lines dated on or before a product's stock count move nothing."""

    def setUp(self):
        self.today = timezone.localdate()
        self.counted = self.today - timedelta(days=10)
        InventoryItem.objects.filter(pk=self.stock.pk).update(stock_counted_at=self.counted)

    def sale(self, day, quantity=3):
        client = client_for(self.owner)
        when = timezone.make_aware(timezone.datetime(day.year, day.month, day.day, 12, 0))
        response = client.post('/sales-invoices/create-with-items/', {
            'customer': self.customer.id, 'date_issued': when.isoformat(), 'invoice_number': f'S-{day}',
            'items': [{'product_id': self.white.id, 'quantity': quantity, 'unit_price': 100}]}, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        return SalesInvoice.objects.get(pk=response.json()['id'])

    def on_hand(self):
        return InventoryItem.objects.get(pk=self.stock.pk).quantity_on_hand

    def test_a_sale_before_the_count_moves_nothing(self):
        invoice = self.sale(self.counted - timedelta(days=30))
        self.assertEqual(self.on_hand(), 50)
        line = invoice.invoice_items.get()
        self.assertTrue(line.stock_skipped and line.is_deducted)
        self.assertFalse(line.restocks.exists())

    def test_a_sale_after_the_count_moves_stock(self):
        self.sale(self.today)
        self.assertEqual(self.on_hand(), 47)

    def test_moving_the_date_across_the_count_moves_exactly_the_ledger(self):
        invoice = self.sale(self.today)
        self.assertEqual(self.on_hand(), 47)
        client = client_for(self.owner)
        before = timezone.make_aware(timezone.datetime(2020, 1, 5, 12, 0))
        client.patch(f'/sales-invoices/{invoice.id}/', {'date_issued': before.isoformat()}, format='json')
        self.assertEqual(self.on_hand(), 50)
        self.assertTrue(invoice.invoice_items.get().stock_skipped)
        client.patch(f'/sales-invoices/{invoice.id}/', {'date_issued': timezone.now().isoformat()},
                     format='json')
        self.assertEqual(self.on_hand(), 47)
        self.assertFalse(invoice.invoice_items.get().stock_skipped)

    def test_a_purchase_follows_its_delivery_date(self):
        client = client_for(self.owner)
        old = (self.counted - timedelta(days=1)).isoformat()
        response = client.post('/purchase-invoices/create-with-items/', {
            'supplier': self.supplier.id, 'status': 'R', 'date_issued': timezone.now().isoformat(),
            'delivery': old, 'invoice_number': 'P-old',
            'items': [{'product_id': self.white.id, 'quantity': 5, 'unit_cost': 10}]}, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self.on_hand(), 50)
        invoice = PurchaseInvoice.objects.get(pk=response.json()['id'])
        client.patch(f'/purchase-invoices/{invoice.id}/', {'delivery': self.today.isoformat()}, format='json')
        self.assertEqual(self.on_hand(), 55)

    def test_a_recount_is_not_reprocessed(self):
        invoice = self.sale(self.today)
        self.assertEqual(self.on_hand(), 47)
        # Counted again today, at 45: the sale is in that count, and editing
        # the invoice afterwards (its date unchanged) moves nothing.
        owner = client_for(self.owner)
        owner.patch(f'/inventory/{self.business.inventory_glance.id}/items/{self.stock.id}/',
                    {'quantity': 45, 'quantity_on_hand': 45}, format='json')
        row = InventoryItem.objects.get(pk=self.stock.pk)
        self.assertEqual((row.quantity_on_hand, row.stock_counted_at), (45, self.today))
        line = invoice.invoice_items.get()
        response = owner.post(f'/sales-invoices/{invoice.id}/update-with-items/', {
            'customer': self.customer.id, 'notes': 'edited', 'invoice_number': invoice.invoice_number,
            'date_issued': invoice.date_issued.isoformat(),
            'items': [{'id': line.id, 'product_id': self.white.id, 'quantity': 3, 'unit_price': 100}]},
            format='json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.on_hand(), 45)
        self.assertFalse(invoice.invoice_items.get().stock_skipped)

    def test_a_price_edit_is_not_a_count(self):
        client_for(self.owner).patch(
            f'/inventory/{self.business.inventory_glance.id}/items/{self.stock.id}/',
            {'unit_price': 9999}, format='json')
        self.assertEqual(InventoryItem.objects.get(pk=self.stock.pk).stock_counted_at, self.counted)

    def test_a_return_before_the_count_adds_nothing(self):
        invoice = self.sale(self.today)
        line = invoice.invoice_items.get()
        returned = ReturnedItem.objects.create(business=self.business, invoice_item=line, quantity=1)
        ReturnedItem.objects.filter(pk=returned.pk).update(
            created_at=timezone.make_aware(timezone.datetime(2020, 1, 1, 12, 0)))
        client_for(self.owner).post(f'/returned-items/{returned.id}/inventory-return/')
        self.assertEqual(self.on_hand(), 47)
