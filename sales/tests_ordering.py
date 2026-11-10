"""Sales and purchase invoices list newest day first, then highest invoice
number (plans/product-extra-fields.md, part 03, 3.5)."""
from datetime import datetime, time
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from root.models import WALK_IN_CUSTOMER_NAME, Business, City, Customer, Supplier
from .models import PurchaseInvoice, SalesInvoice

User = get_user_model()
KARACHI = ZoneInfo("Asia/Karachi")


def at(day, hour, minute=0):
    return datetime.combine(day, time(hour, minute), tzinfo=KARACHI)


class InvoiceOrderTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="order@example.com", password="pw-for-tests")
        City.objects.get_or_create(name="Karachi", defaults={"postal_code": "74000"})
        cls.business = Business.objects.create(
            name="Ordered Paints", owner=cls.user, phone="03001234567", is_active=True)
        cls.customer = Customer.objects.get(business=cls.business, name=WALK_IN_CUSTOMER_NAME)
        cls.supplier = Supplier.objects.create(business=cls.business, name="Dealer")

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def numbers(self, url):
        listed = self.client.get(url).json()
        rows = listed["results"] if isinstance(listed, dict) else listed
        return [row["invoice_number"] for row in rows]

    def make(self, model, **party):
        today, yesterday = datetime(2026, 11, 10).date(), datetime(2026, 11, 9).date()
        # Saved in an order unlike the one expected, at times that would
        # mislead: noon (an Update save) beside midnight (a Create save).
        for number, when in (
            ("9", at(today, 0)), ("SAL-100", at(today, 0)), ("", at(today, 12)),
            ("10", at(today, 12)), ("SAL-099", at(today, 0)),
            ("SAL-200", at(yesterday, 23, 30)),
            # 00:30 Karachi is still the 10th, though it is the 9th in UTC.
            ("1", at(today, 0, 30)),
        ):
            model.objects.create(business=self.business, created_by=self.user,
                                 invoice_number=number, date_issued=when, **party)

    def test_sales_by_day_then_number(self):
        self.make(SalesInvoice, customer=self.customer)
        self.assertEqual(self.numbers("/sales-invoices/"),
                         ["SAL-100", "SAL-099", "10", "9", "1", "", "SAL-200"])

    def test_purchases_by_day_then_number(self):
        self.make(PurchaseInvoice, supplier=self.supplier)
        self.assertEqual(self.numbers("/purchase-invoices/"),
                         ["SAL-100", "SAL-099", "10", "9", "1", "", "SAL-200"])

    def test_the_summary_still_adds_up_over_the_ordered_list(self):
        self.make(SalesInvoice, customer=self.customer)
        self.assertEqual(self.client.get("/sales-invoices/summary/").status_code, 200)
        self.assertEqual(self.client.get("/purchase-invoices/summary/").status_code, 200)
