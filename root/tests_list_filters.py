"""
The list pages' filters (plans/list-filters.md): each narrows its list as the
dialog says, ranges include their ends, and an id filter keeps two records
with the same name apart.
"""
from datetime import date, datetime, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import MoneyAccount, PartyPayment, Transaction
from inventory.models import InventoryItem
from projects.models import Project, ProjectSalesInvoice
from root.models import (
    Business, BusinessConfig, City, Customer, Expense, Location, Product,
    ProductVariant, Supplier, Unit,
)
from sales.models import (
    PurchaseInvoice, PurchaseQuotation, PurchaseQuotationItem, ReturnedItem,
    SalesInvoice, SalesInvoiceItem,
)

User = get_user_model()


def noon(day):
    return timezone.make_aware(datetime(day.year, day.month, day.day, 12, 0))


class ListFilterTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="filters@example.com", password="pw-for-tests")
        cls.city = City.objects.create(name="Sukkur", postal_code="65200")
        cls.city2 = City.objects.create(name="Larkana", postal_code="77150")
        cls.business = Business.objects.create(name="Filter Paints", owner=cls.user,
                                               phone="03001234567", is_active=True)
        BusinessConfig.objects.create(business=cls.business, accounting=True, projects=True)
        cls.today = timezone.localdate()
        # Two customers with the same name: only an id can tell them apart.
        cls.ali_1 = Customer.objects.create(business=cls.business, name="Ali", city=cls.city)
        cls.ali_2 = Customer.objects.create(business=cls.business, name="Ali", city=cls.city2)
        cls.supplier = Supplier.objects.create(business=cls.business, name="Jotun")
        cls.supplier2 = Supplier.objects.create(business=cls.business, name="Berger")
        cls.unit = Unit.objects.create(name="Tins", abv="Tin")
        cls.unit2 = Unit.objects.create(name="Drums", abv="Dr")
        cls.paint = Product.objects.create(business=cls.business, name="Emulsion", unit=cls.unit)
        cls.putty = Product.objects.create(business=cls.business, name="Putty", unit=cls.unit2, is_active=False)
        cls.white = ProductVariant.objects.create(base=cls.paint, name="white", sku="F-1")
        cls.grey = ProductVariant.objects.create(base=cls.putty, name="grey", sku="F-2")

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def ids(self, url, **params):
        response = self.client.get(url, params)
        self.assertEqual(response.status_code, 200, response.content)
        return {row["id"] for row in response.json()}

    def sale(self, number, customer, total):
        return SalesInvoice.objects.create(
            business=self.business, customer=customer, invoice_number=number, created_by=self.user,
            date_issued=noon(self.today), date_due=self.today, total=total, status="C")


class SalesAndPurchaseFilterTests(ListFilterTests):

    def test_customer_by_id_keeps_namesakes_apart(self):
        first = self.sale("S-1", self.ali_1, 100)
        second = self.sale("S-2", self.ali_2, 100)
        self.assertEqual(self.ids("/sales-invoices/", customer=self.ali_1.id), {first.id})
        self.assertEqual(self.ids("/sales-invoices/", customer__name="Ali"), {first.id, second.id})

    def test_total_range_includes_its_ends(self):
        low, mid, high = (self.sale(f"T-{n}", self.ali_1, n) for n in (100, 500, 900))
        self.assertEqual(self.ids("/sales-invoices/", total_min=100, total_max=500), {low.id, mid.id})

    def test_project_and_entered_by(self):
        invoice = self.sale("P-1", self.ali_1, 100)
        self.sale("P-2", self.ali_1, 100)
        project = Project.objects.create(business=self.business, name="Tower")
        ProjectSalesInvoice.objects.create(project=project, sales_invoice=invoice)
        self.assertEqual(self.ids("/sales-invoices/", project=project.id), {invoice.id})
        self.assertEqual(len(self.ids("/sales-invoices/", created_by=self.user.id)), 2)

    def test_the_summary_follows_the_filters(self):
        self.sale("C-1", self.ali_1, 300)
        self.sale("C-2", self.ali_2, 700)
        summary = self.client.get("/sales-invoices/summary/", {"customer": self.ali_2.id}).json()
        self.assertEqual(summary["sales_invoiced"], 700)
        self.assertTrue(summary["filtered"])

    def test_purchases_by_supplier_and_total(self):
        def purchase(number, supplier, total):
            return PurchaseInvoice.objects.create(
                business=self.business, supplier=supplier, invoice_number=number,
                created_by=self.user, date_issued=noon(self.today), total=total, status="D")
        mine = purchase("P-1", self.supplier, 1000)
        purchase("P-2", self.supplier2, 1000)
        purchase("P-3", self.supplier, 9000)
        self.assertEqual(self.ids("/purchase-invoices/", supplier=self.supplier.id, total_max=5000), {mine.id})


class ExpenseFilterTests(ListFilterTests):

    def test_category_amount_and_account(self):
        rent = Expense.objects.create(business=self.business, name="Rent", category="kiraya", amount=50000)
        Expense.objects.create(business=self.business, name="Bill", category="bijli", amount=9000)
        self.assertEqual(self.ids("/expenses/", category="kiraya"), {rent.id})
        self.assertEqual(self.ids("/expenses/", amount_min=10000), {rent.id})
        account = Transaction.objects.get(expense=rent).account
        self.assertIn(rent.id, self.ids("/expenses/", account=account.id))


class ReturnedItemFilterTests(ListFilterTests):

    def test_invoice_number_product_customer_type_and_damage(self):
        invoice = self.sale("INV-7731", self.ali_1, 100)
        other = self.sale("INV-1200", self.ali_2, 100)
        item = SalesInvoiceItem.objects.create(business=self.business, sales_invoice=invoice,
                                               product=self.white, quantity=2, unit_price=50)
        other_item = SalesInvoiceItem.objects.create(business=self.business, sales_invoice=other,
                                                     product=self.grey, quantity=2, unit_price=50)
        mine = ReturnedItem.objects.create(business=self.business, invoice_item=item, quantity=1,
                                           return_type="to_inventory", is_damaged=True)
        ReturnedItem.objects.create(business=self.business, invoice_item=other_item, quantity=1,
                                    return_type="to_invoice")
        self.assertEqual(self.ids("/returned-items/", invoice_number="7731"), {mine.id})
        self.assertEqual(self.ids("/returned-items/", product=self.paint.id), {mine.id})
        self.assertEqual(self.ids("/returned-items/", customer=self.ali_1.id), {mine.id})
        self.assertEqual(self.ids("/returned-items/", return_type="to_inventory"), {mine.id})
        self.assertEqual(self.ids("/returned-items/", is_damaged="true"), {mine.id})


class TransactionFilterTests(ListFilterTests):

    def test_customer_supplier_and_amount(self):
        account = MoneyAccount.objects.create(business=self.business, name="Meezan", type="bank")
        receipt = Transaction.objects.create(business=self.business, account=account,
                                             type="customer_receipt", amount=1200, date=self.today)
        PartyPayment.objects.create(business=self.business, customer=self.ali_1, transaction=receipt)
        paid = Transaction.objects.create(business=self.business, account=account,
                                          type="supplier_payment", amount=8000, date=self.today)
        PartyPayment.objects.create(business=self.business, supplier=self.supplier, transaction=paid)
        self.assertEqual(self.ids("/transactions/", customer=self.ali_1.id), {receipt.id})
        self.assertEqual(self.ids("/transactions/", supplier=self.supplier.id), {paid.id})
        self.assertEqual(self.ids("/transactions/", amount_min=5000, account=account.id), {paid.id})


class StockAndCatalogueFilterTests(ListFilterTests):

    def test_stock_by_product_location_level_and_price(self):
        shop = Location.objects.create(business=self.business, name="Shop")
        store = Location.objects.create(business=self.business, name="Store")
        inventory = self.business.inventory_glance
        out = InventoryItem.objects.create(business=self.business, inventory=inventory, product=self.white,
                                           location=shop, quantity_on_hand=0, unit_price=100)
        low = InventoryItem.objects.create(business=self.business, inventory=inventory, product=self.grey,
                                           location=store, quantity_on_hand=3, reorder_level=5, unit_price=400)
        url = f"/inventory/{inventory.id}/items/"
        self.assertEqual(self.ids(url, product=self.paint.id), {out.id})
        self.assertEqual(self.ids(url, location=store.id), {low.id})
        self.assertEqual(self.ids(url, stock_level="out"), {out.id})
        self.assertEqual(self.ids(url, stock_level="low"), {low.id})
        self.assertEqual(self.ids(url, stock_level="in_stock"), set())
        self.assertEqual(self.ids(url, price_min=200), {low.id})

    def test_products_by_unit_and_status(self):
        self.assertEqual(self.ids("/products/", unit=self.unit.id), {self.paint.id})
        self.assertEqual(self.ids("/products/", is_active="false"), {self.putty.id})

    def test_customers_by_city(self):
        self.assertIn(self.ali_2.id, self.ids("/customers/", city=self.city2.id))
        self.assertNotIn(self.ali_1.id, self.ids("/customers/", city=self.city2.id))


class ProjectAndQuotationFilterTests(ListFilterTests):

    def test_projects_filter_and_search(self):
        tower = Project.objects.create(business=self.business, name="Tower", customer=self.ali_1, status="IP")
        Project.objects.create(business=self.business, name="Villa", customer=self.ali_2, status="P")
        self.assertEqual(self.ids("/projects/", customer=self.ali_1.id), {tower.id})
        self.assertEqual(self.ids("/projects/", status="IP"), {tower.id})
        self.assertEqual(self.ids("/projects/", search="tow"), {tower.id})

    def test_quotations_by_status_supplier_project_and_date(self):
        sent = PurchaseQuotation.objects.create(business=self.business, quotation_no="Q-1",
                                                created_by=self.user, status="S")
        PurchaseQuotation.objects.create(business=self.business, quotation_no="Q-2",
                                         created_by=self.user, status="D")
        PurchaseQuotationItem.objects.create(business=self.business, purchase_quotation=sent,
                                             supplier=self.supplier, product=self.white,
                                             quantity=2, unit_price=10)
        PurchaseQuotationItem.objects.create(business=self.business, purchase_quotation=sent,
                                             supplier=self.supplier, product=self.grey,
                                             quantity=1, unit_price=10)
        self.assertEqual(self.ids("/purchase-quotations/", status="S"), {sent.id})
        # Two items from one supplier still list the quotation once.
        response = self.client.get("/purchase-quotations/", {"supplier": self.supplier.id}).json()
        self.assertEqual([row["id"] for row in response], [sent.id])
        tomorrow = (self.today + timedelta(days=1)).isoformat()
        self.assertEqual(self.ids("/purchase-quotations/", created_from=tomorrow), set())
