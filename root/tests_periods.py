"""
Period switches: one rule for what a period covers, the list tables and the
cards that summarise them following it together, and the Book of Business.
"""
from datetime import date, datetime, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import MoneyAccount, Transaction
from root.models import (
    WALK_IN_CUSTOMER_NAME, Business, BusinessConfig, City, Customer, Expense,
    Supplier,
)
from root.periods import period_bounds
from sales.models import PurchaseInvoice, SalesInvoice, SalesReceipt

User = get_user_model()


class PeriodBoundsTests(TestCase):
    """What each period covers. Every period ends today."""

    def test_the_week_starts_on_saturday(self):
        # 2026-10-17 is a Saturday.
        saturday = date(2026, 10, 17)
        for offset in range(7):
            today = saturday + timedelta(days=offset)
            self.assertEqual(period_bounds('week', today), (saturday, today))

    def test_friday_still_belongs_to_the_week_that_began_on_saturday(self):
        friday = date(2026, 10, 23)
        self.assertEqual(period_bounds('week', friday), (date(2026, 10, 17), friday))

    def test_quarters_follow_the_calendar(self):
        self.assertEqual(period_bounds('quarter', date(2026, 2, 14)), (date(2026, 1, 1), date(2026, 2, 14)))
        self.assertEqual(period_bounds('quarter', date(2026, 10, 20)), (date(2026, 10, 1), date(2026, 10, 20)))
        self.assertEqual(period_bounds('quarter', date(2026, 12, 31)), (date(2026, 10, 1), date(2026, 12, 31)))

    def test_month_year_today_and_all(self):
        today = date(2026, 10, 20)
        self.assertEqual(period_bounds('today', today), (today, today))
        self.assertEqual(period_bounds('month', today), (date(2026, 10, 1), today))
        self.assertEqual(period_bounds('year', today), (date(2026, 1, 1), today))
        self.assertEqual(period_bounds('all', today), (None, None))

    def test_custom_needs_an_ordered_pair_of_dates(self):
        first, last = date(2026, 10, 1), date(2026, 10, 15)
        self.assertEqual(period_bounds('custom', date_from=first, date_to=last), (first, last))
        with self.assertRaises(ValueError):
            period_bounds('custom', date_from=first)
        with self.assertRaises(ValueError):
            period_bounds('custom', date_from=last, date_to=first)
        with self.assertRaises(ValueError):
            period_bounds('fortnight')


class PeriodSwitchTests(TestCase):
    """The table and its cards follow the same period and the same filters."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="periods@example.com", password="pw-for-tests")
        city = City.objects.create(name="Sukkur", postal_code="65200")
        cls.business = Business.objects.create(
            name="Period Paints", owner=cls.user, phone="03001234567", is_active=True)
        BusinessConfig.objects.create(business=cls.business, accounting=True)
        cls.walk_in = Customer.objects.get(business=cls.business, name=WALK_IN_CUSTOMER_NAME)
        cls.ali = Customer.objects.create(business=cls.business, name="Ali Traders", city=city)
        cls.supplier = Supplier.objects.create(business=cls.business, name="Jotun")
        cls.cash = MoneyAccount.objects.get_default_account(cls.business.id)
        cls.today = timezone.localdate()
        cls.last_year = cls.today.replace(year=cls.today.year - 1)

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def at(self, day):
        return timezone.make_aware(datetime(day.year, day.month, day.day, 12, 0))

    def sale(self, number, total, day, customer=None, status='C'):
        return SalesInvoice.objects.create(
            business=self.business, customer=customer or self.walk_in,
            invoice_number=number, total=total, sub_total=total, created_by=self.user,
            status=status, date_issued=self.at(day), date_due=self.at(day))

    def money(self, kind, amount, day):
        return Transaction.objects.create(
            business=self.business, account=self.cash, type=kind,
            amount=amount, date=day, status='C')

    def get(self, url, **params):
        response = self.client.get(url, params)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    # -- sales ------------------------------------------------------------

    def test_sales_summary_follows_the_period_and_leaves_out_cancelled(self):
        self.sale('S-1', 1000, self.today)
        self.sale('S-2', 3000, self.today)
        self.sale('S-X', 9999, self.today, status='X')
        self.sale('S-OLD', 5000, self.last_year)

        today = self.get('/sales-invoices/summary/', period='today')
        self.assertEqual(today['sales_invoiced'], 4000)
        self.assertEqual(today['invoices_raised'], 2)
        self.assertEqual(today['average_order_value'], 2000)
        self.assertEqual(today['period']['key'], 'today')

        everything = self.get('/sales-invoices/summary/')
        self.assertEqual(everything['sales_invoiced'], 9000)
        self.assertEqual(everything['period']['key'], 'all')

    def test_the_list_and_the_summary_cover_the_same_rows(self):
        self.sale('S-1', 1000, self.today)
        self.sale('S-OLD', 5000, self.last_year)

        rows = self.get('/sales-invoices/', period='today')
        self.assertEqual([row['invoice_number'] for row in rows], ['S-1'])
        self.assertEqual(len(self.get('/sales-invoices/')), 2)

    def test_payments_received_counts_khaata_receipts_only_when_unfiltered(self):
        mine = self.sale('S-ALI', 2000, self.today, customer=self.ali)
        self.sale('S-WALK', 1000, self.today)
        receipt = SalesReceipt.objects.create(sales_invoice=mine, amount=700)
        Transaction.objects.filter(sales_receipt=receipt).update(
            date=self.today, status='C', account=self.cash)
        self.money('customer_receipt', 300, self.today)

        unfiltered = self.get('/sales-invoices/summary/', period='today')
        self.assertEqual(unfiltered['payments_received'], 1000)
        self.assertFalse(unfiltered['filtered'])

        searched = self.get('/sales-invoices/summary/', period='today', search='Ali')
        self.assertTrue(searched['filtered'])
        self.assertEqual(searched['sales_invoiced'], 2000)
        self.assertEqual(searched['payments_received'], 700)

    def test_a_custom_range_and_a_bad_period(self):
        self.sale('S-1', 1000, self.today)
        self.sale('S-OLD', 5000, self.last_year)
        start = self.last_year - timedelta(days=1)
        end = self.last_year + timedelta(days=1)

        custom = self.get('/sales-invoices/summary/', period='custom',
                          date_from=start.isoformat(), date_to=end.isoformat())
        self.assertEqual(custom['sales_invoiced'], 5000)

        self.assertEqual(self.client.get('/sales-invoices/summary/', {'period': 'fortnight'}).status_code, 400)
        self.assertEqual(self.client.get('/sales-invoices/', {'period': 'custom'}).status_code, 400)

    # -- purchases, expenses, transactions -------------------------------

    def test_purchase_summary(self):
        for number, total, day, status in (('P-1', 4000, self.today, 'R'),
                                           ('P-X', 999, self.today, 'C'),
                                           ('P-OLD', 7000, self.last_year, 'R')):
            PurchaseInvoice.objects.create(
                business=self.business, supplier=self.supplier, invoice_number=number,
                created_by=self.user,
                total=total, sub_total=total, status=status, date_issued=self.at(day))
        self.money('supplier_payment', 2500, self.today)

        month = self.get('/purchase-invoices/summary/', period='month')
        self.assertEqual(month['purchases_invoiced'], 4000)
        self.assertEqual(month['invoices_raised'], 1)
        self.assertEqual(month['payments_made'], 2500)

    def test_expense_summary_goes_by_when_it_was_paid(self):
        Expense.objects.create(business=self.business, name="Rent", category="kiraya", amount=50000)
        old = Expense.objects.create(business=self.business, name="Bill", category="bijli", amount=9000)
        # Entered today, paid last year: the paid date decides.
        Transaction.objects.filter(expense=old).update(date=self.last_year)

        today = self.get('/expenses/summary/', period='today')
        self.assertEqual((today['expense_count'], today['expense_amount']), (1, 50000))
        everything = self.get('/expenses/summary/')
        self.assertEqual(everything['expense_count'], 2)

    def test_transaction_summary_separates_in_and_out_and_skips_pending(self):
        self.money('customer_receipt', 1200, self.today)
        self.money('expense', 400, self.today)
        Transaction.objects.create(
            business=self.business, account=self.cash, type='customer_receipt',
            amount=5000, date=self.today, status='P')
        self.money('customer_receipt', 8000, self.last_year)

        today = self.get('/transactions/summary/', period='today')
        self.assertEqual((today['money_in'], today['money_out']), (1200, 400))
        receipts = self.get('/transactions/summary/', period='today', type='customer_receipt')
        self.assertEqual((receipts['money_in'], receipts['money_out']), (1200, 0))

    # -- dashboard charts, party card, book of business ------------------

    def test_trends_are_cut_into_buckets_for_their_period(self):
        week = self.get('/sales-kpis/sales-trend/', period='week')['trend']
        start, _ = period_bounds('week', self.today)
        self.assertEqual(len(week), (self.today - start).days + 1)

        year = self.get('/expenses-kpis/expenses-trend/', period='year')['trend']
        self.assertEqual(len(year), self.today.month)

        hours = self.get('/sales-kpis/sales-trend/', period='today')['trend']
        self.assertEqual(len(hours), timezone.localtime().hour + 1)

        self.assertEqual(self.client.get('/sales-kpis/sales-trend/', {'period': 'all'}).status_code, 400)

    def test_total_business_follows_the_card_period(self):
        self.sale('S-1', 1000, self.today, customer=self.ali)
        self.sale('S-OLD', 5000, self.last_year, customer=self.ali)

        self.assertEqual(self.get(f'/customers/{self.ali.id}/summary/')['total_business'], 6000)
        self.assertEqual(
            self.get(f'/customers/{self.ali.id}/summary/', period='year')['total_business'], 1000)

    def test_book_of_business_covers_a_range(self):
        yesterday = self.today - timedelta(days=1)
        self.money('customer_receipt', 1000, yesterday)
        self.money('customer_receipt', 2000, self.today)

        one_day = self.get('/accounting-kpis/day-book/', date=self.today.isoformat())['day_book']
        self.assertEqual(one_day['total_money_in'], 2000)

        both = self.get('/accounting-kpis/day-book/', period='custom',
                        date_from=yesterday.isoformat(), date_to=self.today.isoformat())['day_book']
        self.assertEqual(both['total_money_in'], 3000)
        cash = [section for section in both['accounts'] if section['account_id'] == self.cash.id][0]
        self.assertEqual(cash['closing_balance'] - cash['opening_balance'], 3000)
        self.assertEqual({row['date'] for row in cash['rows']},
                         {yesterday.isoformat(), self.today.isoformat()})

        summary = self.get('/accounting-kpis/daily-summary/', period='custom',
                           date_from=yesterday.isoformat(), date_to=self.today.isoformat())['daily_summary']
        self.assertEqual(summary['money_in']['udhaar_recovered'], 3000)
        self.assertEqual(summary['date_to'], self.today.isoformat())

        self.assertEqual(self.client.get('/accounting-kpis/day-book/', {'period': 'all'}).status_code, 400)
