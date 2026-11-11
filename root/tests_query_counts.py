"""
Lists cost the same number of queries whatever their length
(plans/performance.md, part 01).

Each list is read with 2 rows and again with 10. Every row used to cost its
own queries for the things it nests (its customer, product, supplier,
balance), which is what made the sales list slow enough to break the pipe on
production. The count must not move with the rows.

Also pinned here: the save paths re-total an invoice once and write their
audit events in one INSERT, and an account's balance in one query is the
figure the five separate sums gave.
"""
import datetime

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from accounts.models import MoneyAccount, PartyOpeningBalance, Transaction
from inventory.models import InventoryItem
from projects.models import Project, ProjectSalesInvoice
from root.models import (
    Business, BusinessConfig, City, Customer, Location, Product, ProductVariant, Supplier, Unit,
)
from sales.models import PurchaseInvoice, PurchaseInvoiceItem, SalesInvoice, SalesInvoiceItem

User = get_user_model()
DAY = datetime.date(2026, 5, 10)


class QueryCountTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email='counts@example.com', password='pw')
        cls.business = Business.objects.create(
            name='Count Paints', owner=cls.user, phone='0300', is_active=True)
        BusinessConfig.objects.update_or_create(
            business=cls.business, defaults={'accounting': True, 'projects': True})
        cls.city = City.objects.create(name='Hyderabad', postal_code='71000')
        cls.unit = Unit.objects.create(name='Tin', abv='Tin')
        cls.shop = Location.objects.create(
            business=cls.business, name='Shop', address='-', is_default=True)
        cls.made = 0

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    # -- rows -------------------------------------------------------------

    def add_rows(self, n):
        """n more of everything the lists show, each with what it nests."""
        b = self.business
        for _ in range(n):
            type(self).made += 1
            i = self.made
            customer = Customer.objects.create(business=b, name=f'Customer {i}', city=self.city)
            supplier = Supplier.objects.create(business=b, name=f'Supplier {i}')
            product = Product.objects.create(business=b, name=f'Paint {i}', unit=self.unit)
            variant = ProductVariant.objects.create(base=product, name='White', sku=f'QC-{i}')
            InventoryItem.objects.create(
                business=b, inventory=b.inventory_glance, product=variant, location=self.shop,
                quantity=50, quantity_on_hand=50, unit_price=1000, unit_cost=800)

            sale = SalesInvoice.objects.create(
                business=b, customer=customer, created_by=self.user, invoice_number=f'S-{i}',
                date_issued=datetime.datetime(2026, 5, 10, 12), status='C',
                tax={'value': 0, 'type': 'percentage'}, discount={'value': 0, 'type': 'amount'})
            SalesInvoiceItem.objects.create(
                business=b, sales_invoice=sale, product=variant, quantity=1, unit_price=1000)
            purchase = PurchaseInvoice.objects.create(
                business=b, supplier=supplier, created_by=self.user, invoice_number=f'P-{i}',
                status='D', tax={'value': 0, 'type': 'percentage'})
            PurchaseInvoiceItem.objects.create(
                business=b, purchase_invoice=purchase, product=variant, quantity=2, unit_cost=500)

            project = Project.objects.create(business=b, name=f'Project {i}', customer=customer)
            ProjectSalesInvoice.objects.create(project=project, sales_invoice=sale)

            account = MoneyAccount.objects.create(business=b, name=f'Wallet {i}', type='wallet')
            Transaction.objects.create(
                business=b, account=account, type='other_income', amount=100 * i,
                date=DAY, status='C', payment_method='wallet')
            PartyOpeningBalance.objects.create(
                business=b, customer=customer, amount=5000, as_of_date=datetime.date(2026, 1, 1))
        return sale

    def queries(self, url):
        with CaptureQueriesContext(connection) as q:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200, (url, response.content[:300]))
        return len(q.captured_queries)

    def assert_flat(self, *urls):
        """The same count at 2 rows as at 10."""
        self.add_rows(2)
        sale = SalesInvoice.objects.filter(business=self.business).latest('id')
        inventory = self.business.inventory_glance.id
        expand = lambda url: url.format(inventory=inventory, sale=sale.id, business=self.business.id)
        few = {url: self.queries(expand(url)) for url in urls}
        self.add_rows(8)
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.queries(expand(url)), few[url], url)

    # -- lists --------------------------------------------------------------

    def test_lists_do_not_query_per_row(self):
        self.assert_flat(
            '/sales-invoices/',
            '/purchase-invoices/',
            '/inventory/{inventory}/items/',
            '/products/',
            '/customers/',
            '/suppliers/',
            '/projects/',
            '/money-accounts/',
            '/accounting-kpis/cash-in-hand/',
            '/accounting-kpis/day-book/?date=2026-05-10',
            '/accounting-kpis/opening-balances/?party=customer',
            '/accounting-kpis/daily-summary/?date=2026-05-10',
        )

    # -- balances -------------------------------------------------------------

    def test_balance_is_the_five_sums(self):
        """Every kind of movement, cleared or not, before and after a date."""
        b = self.business
        cash = MoneyAccount.objects.create(business=b, name='Till', opening_balance=1000)
        bank = MoneyAccount.objects.create(business=b, name='Bank', type='bank', opening_balance=50)

        def txn(account, kind, amount, day, status='C', to=None):
            Transaction.objects.create(
                business=b, account=account, transfer_account=to, type=kind, amount=amount,
                date=datetime.date(2026, 5, day), status=status, payment_method='cash')

        txn(cash, 'sale_payment', 700, 1)                 # in
        txn(cash, 'expense', 200, 2)                      # out
        txn(cash, 'transfer', 300, 3, to=bank)            # out of cash, into bank
        txn(cash, 'cash_adjustment', -40, 4)              # signed
        txn(cash, 'cash_adjustment', 15, 6)
        txn(cash, 'customer_receipt', 999, 5, status='PEN')   # not cleared
        txn(bank, 'supplier_payment', 20, 5)

        self.assertEqual(cash.balance(), 1000 + 700 - 200 - 300 - 40 + 15)
        self.assertEqual(bank.balance(), 50 + 300 - 20)
        self.assertEqual(cash.balance(datetime.date(2026, 5, 3)), 1000 + 700 - 200 - 300)
        self.assertEqual(bank.balance(datetime.date(2026, 5, 2)), 50)
        self.assertEqual(cash.balance(datetime.date(2026, 4, 30)), 1000)

        listed = {a.id: a.balance_value for a in MoneyAccount.objects.filter(business=b).with_balance()}
        self.assertEqual(listed[cash.id], cash.balance())
        self.assertEqual(listed[bank.id], bank.balance())
        with self.assertNumQueries(1):
            cash.balance()

    # -- save paths -----------------------------------------------------------

    def test_a_sale_is_totalled_once_and_audited_in_one_insert(self):
        self.add_rows(10)
        variants = list(ProductVariant.objects.filter(base__business=self.business))
        customer = Customer.objects.filter(business=self.business).first()
        with CaptureQueriesContext(connection) as q:
            response = self.client.post('/sales-invoices/create-with-items/', {
                'invoice_number': 'S-MANY', 'customer': customer.id,
                'date_issued': '2026-05-10', 'date_due': '2026-05-10',
                'discount': {'value': 0, 'type': 'amount'}, 'tax': {'value': 0, 'type': 'percentage'},
                'project': None,
                'items': [{'product_id': v.id, 'quantity': 1, 'unit_price': 1000} for v in variants],
            }, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        sql = [x['sql'] for x in q.captured_queries]
        retotals = [s for s in sql if s.startswith('UPDATE "sales_salesinvoice" SET "sub_total"')]
        audits = [s for s in sql if s.startswith('INSERT INTO "audit_auditevent"')]
        self.assertEqual(len(retotals), 1)
        self.assertEqual(len(audits), 1)
        # Every line still left stock.
        self.assertTrue(all(row.quantity_on_hand == 49 for row in
                            InventoryItem.objects.filter(business=self.business)))
