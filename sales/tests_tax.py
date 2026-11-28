"""
Fix plan 5.1-5.3: sales tax is not income, purchase tax is stock cost, and a
percentage tax is charged after the invoice's discount on new invoices.
"""
from datetime import date

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.utils import profit_estimate
from inventory.models import InventoryItem
from root.models import WALK_IN_CUSTOMER_NAME, Business, City, Customer, Product, ProductVariant, Supplier, Unit

from .models import PurchaseInvoice, SalesInvoice
from .utils import stock_unit_cost

User = get_user_model()
FAR_FUTURE = "2099-12-31"


class TaxFixture(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="tax@example.com", password="pw-for-tests")
        City.objects.create(name="Hyderabad", postal_code="71000")
        cls.business = Business.objects.create(
            name="Taxed Paints", owner=cls.user, phone="03001234567", is_active=True)
        cls.customer = Customer.objects.get(
            business=cls.business, person__name=WALK_IN_CUSTOMER_NAME)
        cls.supplier = Supplier.objects.create(business=cls.business, name="Tax Supply Co")
        unit = Unit.objects.create(name="Tins", abv="Tin")
        product = Product.objects.create(business=cls.business, name="Enamel 4L", unit=unit)
        cls.white = ProductVariant.objects.create(base=product, name="white", sku="TAX-1")
        cls.black = ProductVariant.objects.create(base=product, name="black", sku="TAX-2")

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def sell(self, number, price=100000, discount=None, tax=None, day="2026-05-10"):
        response = self.client.post("/sales-invoices/create-with-items/", {
            "invoice_number": number,
            "customer": str(self.customer.id),
            "date_issued": day,
            "date_due": FAR_FUTURE,
            "discount": discount or {"value": 0, "type": "amount"},
            "tax": tax or {"value": 0, "type": "percentage"},
            "extra_charges": [],
            "project": None,
            "items": [{"product_id": self.white.id, "quantity": 1, "unit_price": price}],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        return SalesInvoice.objects.get(business=self.business, invoice_number=number)

    def buy(self, number, items, tax, status="R"):
        response = self.client.post("/purchase-invoices/create-with-items/", {
            "invoice_number": number,
            "supplier": self.supplier.id,
            "date_issued": "2026-05-01T12:00:00",
            "date_due": FAR_FUTURE,
            "delivery": "2026-05-01",
            "status": status,
            "notes": "",
            "tax": tax,
            "project": None,
            "items": items,
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        return PurchaseInvoice.objects.get(business=self.business, invoice_number=number)


class TaxAfterDiscountTests(TaxFixture):
    """5.3: 100,000 less 10,000 discount, 17% tax: tax on 90,000."""

    def test_a_new_invoice_charges_tax_after_the_discount(self):
        invoice = self.sell("T-1", discount={"value": 10000, "type": "amount"},
                            tax={"value": 17, "type": "percentage"})
        self.assertTrue(invoice.tax_after_discount)
        self.assertAlmostEqual(invoice.tax_total, 15300)
        self.assertAlmostEqual(invoice.total, 105300)

    def test_an_invoice_made_under_the_old_rule_keeps_its_total(self):
        invoice = self.sell("T-2", discount={"value": 10000, "type": "amount"},
                            tax={"value": 17, "type": "percentage"})
        SalesInvoice.objects.filter(pk=invoice.pk).update(tax_after_discount=False)
        invoice.refresh_from_db()
        invoice.adjust_totals()
        invoice.refresh_from_db()
        self.assertAlmostEqual(invoice.tax_total, 17000)
        self.assertAlmostEqual(invoice.total, 107000)

    def test_a_fixed_tax_is_unchanged(self):
        invoice = self.sell("T-3", discount={"value": 10, "type": "percentage"},
                            tax={"value": 500, "type": "amount"})
        self.assertAlmostEqual(invoice.tax_total, 500)
        self.assertAlmostEqual(invoice.total, 100000 - 10000 + 500)

    def test_the_screens_get_the_rule_and_the_tax(self):
        invoice = self.sell("T-4", tax={"value": 10, "type": "percentage"})
        body = self.client.get(f"/sales-invoices/{invoice.id}/").json()
        self.assertEqual((body["tax_total"], body["tax_after_discount"]), (10000, True))


class SalesTaxIsNotIncomeTests(TaxFixture):
    """5.1: revenue leaves out the tax; it is reported beside it."""

    def test_profit_leaves_the_tax_out_of_revenue(self):
        self.sell("T-10", tax={"value": 17, "type": "percentage"})
        self.sell("T-11", price=50000)
        figures = profit_estimate(self.business.id, date(2026, 5, 1), date(2026, 5, 31))
        self.assertEqual(figures["revenue"], 150000)
        self.assertEqual(figures["sales_tax"], 17000)

    def test_a_cancelled_invoice_collects_no_tax(self):
        invoice = self.sell("T-12", tax={"value": 17, "type": "percentage"})
        SalesInvoice.objects.filter(pk=invoice.pk).update(status="X")
        figures = profit_estimate(self.business.id, date(2026, 5, 1), date(2026, 5, 31))
        self.assertEqual((figures["revenue"], figures["sales_tax"]), (0, 0))

    def test_the_migration_fills_old_invoices_by_the_old_rule(self):
        from importlib import import_module
        fill = import_module("sales.migrations.0037_sales_tax_total_and_rule").fill_tax_total
        percent = self.sell("T-13", discount={"value": 10000, "type": "amount"},
                            tax={"value": 17, "type": "percentage"})
        fixed = self.sell("T-14", tax={"value": 2, "type": "amount"})
        none = self.sell("T-15")
        SalesInvoice.objects.filter(pk__in=[percent.pk, fixed.pk, none.pk]).update(tax_total=0)
        fill(apps, None)
        # 17% of the stored sub_total (before the discount), the old rule.
        self.assertEqual([round(SalesInvoice.objects.get(pk=i.pk).tax_total) for i in (percent, fixed, none)],
                         [17000, 2, 0])


class PurchaseTaxIsStockCostTests(TaxFixture):
    """5.2: a purchase's tax is shared across its lines by value."""

    def test_received_stock_carries_its_share_of_the_tax(self):
        self.buy("P-1", [
            {"product_id": self.white.id, "quantity": 10, "unit_cost": 6000},   # 60,000
            {"product_id": self.black.id, "quantity": 20, "unit_cost": 2000},   # 40,000
        ], tax={"value": 10, "type": "percentage"})
        white = InventoryItem.objects.get(business=self.business, product=self.white)
        black = InventoryItem.objects.get(business=self.business, product=self.black)
        self.assertAlmostEqual(white.unit_cost, 6600)
        self.assertAlmostEqual(black.unit_cost, 2200)

    def test_a_fixed_tax_is_shared_by_value(self):
        invoice = self.buy("P-2", [
            {"product_id": self.white.id, "quantity": 10, "unit_cost": 6000},
            {"product_id": self.black.id, "quantity": 20, "unit_cost": 2000},
        ], tax={"value": 1000, "type": "amount"}, status="D")
        white, black = invoice.invoice_items.order_by("id")
        # 600 to the 60,000 line (60 a unit), 400 to the 40,000 line (20 a unit).
        self.assertAlmostEqual(stock_unit_cost(white, invoice), 6060)
        self.assertAlmostEqual(stock_unit_cost(black, invoice), 2020)

    def test_no_tax_keeps_the_price(self):
        self.buy("P-3", [{"product_id": self.white.id, "quantity": 5, "unit_cost": 1000}],
                 tax={"value": 0, "type": "percentage"})
        self.assertEqual(
            InventoryItem.objects.get(business=self.business, product=self.white).unit_cost, 1000)

    def test_a_line_discount_comes_off_before_the_tax_share(self):
        self.buy("P-4", [{"product_id": self.white.id, "quantity": 10, "unit_cost": 100,
                          "discount": {"type": "percentage", "value": 10}}],
                 tax={"value": 10, "type": "percentage"})
        # 90 a unit after the discount, plus 10% tax on it.
        self.assertAlmostEqual(
            InventoryItem.objects.get(business=self.business, product=self.white).unit_cost, 99)
