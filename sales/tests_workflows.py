"""
What happens after an invoice is saved, held to the same rules whichever
screen submits it (docs/INVOICE-WORKFLOWS.md).

  - A sales line taken off by an update gives its stock back.
  - A create is all or nothing: a failure while moving stock leaves no
    half-processed invoice behind.
  - A rejected purchase says why, as a sales one does.
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from inventory.models import InventoryItem
from root.models import Business, City, Customer, Location, Product, ProductVariant, Supplier, Unit
from sales.models import PurchaseInvoice, SalesInvoice

User = get_user_model()


class InvoiceWorkflowTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="workflows@example.com", password="pw-for-tests")
        City.objects.create(name="Hyderabad", postal_code="71000")
        cls.business = Business.objects.create(
            name="Workflow Paints", owner=cls.user, phone="03001234567", is_active=True)
        cls.customer = Customer.objects.filter(business=cls.business).first() or Customer.objects.create(
            business=cls.business, name="Walk-in", city=City.objects.first())
        cls.supplier = Supplier.objects.create(business=cls.business, name="Jotun")
        cls.unit = Unit.objects.create(name="Tin", abv="Tin")
        cls.shop = Location.objects.create(business=cls.business, name="Shop", address="-", is_default=True)
        product = Product.objects.create(business=cls.business, name="Emulsion", unit=cls.unit)
        cls.white = ProductVariant.objects.create(base=product, name="White", sku="WF-1")
        cls.blue = ProductVariant.objects.create(base=product, name="Blue", sku="WF-2")
        for variant in (cls.white, cls.blue):
            InventoryItem.objects.create(
                business=cls.business, inventory=cls.business.inventory_glance, product=variant,
                location=cls.shop, quantity=50, quantity_on_hand=50, unit_price=1000, unit_cost=800)

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def on_hand(self, variant):
        return InventoryItem.objects.get(product=variant).quantity_on_hand

    def sale(self, items, number="W-1"):
        return {
            "invoice_number": number, "customer": self.customer.id,
            "date_issued": "2026-05-10", "date_due": "2026-05-10",
            "discount": {"value": 0, "type": "amount"}, "tax": {"value": 0, "type": "percentage"},
            "project": None, "items": items,
        }

    def test_a_line_taken_off_a_sale_gives_its_stock_back(self):
        created = self.client.post("/sales-invoices/create-with-items/", self.sale([
            {"product_id": self.white.id, "quantity": 4, "unit_price": 1000},
            {"product_id": self.blue.id, "quantity": 3, "unit_price": 1000},
        ]), format="json")
        self.assertEqual(created.status_code, 201, created.content)
        invoice = SalesInvoice.objects.get(pk=created.json()["id"])
        self.assertEqual((self.on_hand(self.white), self.on_hand(self.blue)), (46, 47))

        white_line = invoice.invoice_items.get(product=self.white)
        updated = self.client.post(f"/sales-invoices/{invoice.pk}/update-with-items/", self.sale([
            {"id": white_line.id, "product_id": self.white.id, "quantity": 4, "unit_price": 1000},
        ]), format="json")
        self.assertEqual(updated.status_code, 200, updated.content)
        # Blue came off the bill: its 3 are back; white is untouched.
        self.assertEqual((self.on_hand(self.white), self.on_hand(self.blue)), (46, 50))

    def test_a_sale_that_fails_part_way_leaves_nothing(self):
        with mock.patch("sales.serializers.updateInventoryOnSale", side_effect=RuntimeError("stock down")):
            response = self.client.post("/sales-invoices/create-with-items/", self.sale(
                [{"product_id": self.white.id, "quantity": 2, "unit_price": 1000}]), format="json")
        self.assertEqual(response.status_code, 500)
        self.assertFalse(SalesInvoice.objects.filter(invoice_number="W-1").exists())
        self.assertEqual(self.on_hand(self.white), 50)

    def test_a_rejected_purchase_says_why(self):
        response = self.client.post("/purchase-invoices/create-with-items/", {
            "supplier": self.supplier.id, "status": "R", "date_issued": "2026-05-10",
            "items": "not a list"}, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("items", response.json())
        self.assertFalse(PurchaseInvoice.objects.exists())

    def test_the_sales_picker_can_list_what_has_run_out(self):
        """An out-of-stock product, and one never stocked, are listed with 0
        on hand when asked for - so a sale shows them (disabled) instead of
        inviting a duplicate - and left out otherwise."""
        InventoryItem.objects.filter(product=self.blue).update(quantity_on_hand=0, quantity=0)
        never = ProductVariant.objects.create(base=self.white.base, name="Green", sku="WF-3")
        url = f"/inventory/{self.business.id}/items/get_available_items/"

        listed = {row["product"]["id"]: row["available_quantity"] for row in self.client.get(url).json()}
        self.assertEqual(listed, {self.white.id: 50})

        listed = {row["product"]["id"]: row["available_quantity"]
                  for row in self.client.get(url + "?include_out_of_stock=1").json()}
        self.assertEqual(listed, {self.white.id: 50, self.blue.id: 0, never.id: 0})

