import os
import tempfile
from datetime import date
from io import BytesIO

from PIL import Image

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from rest_framework.test import APIClient

from core.admin_masking import MASK
from root.models import (
    WALK_IN_CUSTOMER_NAME, Business, City, Customer, Product,
    ProductVariant, Supplier, Unit,
)

from inventory.models import Inventory, InventoryItem
from .models import (
    InvoiceImage, PurchaseInvoice, PurchaseInvoiceItem, SalesInvoice,
    SalesInvoiceItem, SalesInvoiceItemDeduction,
)
from accounts.utils import party_invoiced, profit_estimate
from .models import SalesReceipt
from .utils import build_whatsapp_payload, invoice_room, updateInventoryOnSale

User = get_user_model()

# A due date no test run will ever be past, so an unpaid invoice reads as
# Completed rather than deriving to Overdue on the day the suite runs.
FAR_FUTURE = "2099-12-31"

# Unlike any pk, quantity or date on the page, so a plain substring check is
# enough to prove absence.
COST_CANARY = 7654321
TOTAL_CANARY = 9876543


class MaskedAmountAdminTests(TestCase):
    """The NOT NULL amount: masked, editable, and required only on add."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser(
            email="sales-masking@example.com", password="pw-for-tests"
        )
        cls.business = Business.objects.create(
            name="Canary Traders", owner=cls.user, phone="03001234567"
        )
        cls.supplier = Supplier.objects.create(
            business=cls.business, name="Canary Supply Co"
        )
        # status is passed explicitly for two reasons: the model default is the
        # whole choices tuple rather than its code, and 'R'/'PR' would fire
        # updateInventoryOnPurchase, whose morph() needs a real ProductVariant.
        # A draft invoice keeps this test on the masking behaviour.
        cls.invoice = PurchaseInvoice.objects.create(
            business=cls.business,
            supplier=cls.supplier,
            created_by=cls.user,
            status="D",
        )
        cls.item = PurchaseInvoiceItem.objects.create(
            business=cls.business,
            purchase_invoice=cls.invoice,
            quantity=5,
            unit_cost=COST_CANARY,
        )
        # total is derived: sales.signals.updateTotalsAfterPurchaseInvoiceItem
        # recomputes it on every item save, so it has to be planted afterwards
        # and through the queryset, which bypasses save() and its signals.
        PurchaseInvoice.objects.filter(pk=cls.invoice.pk).update(total=TOTAL_CANARY)
        cls.invoice.refresh_from_db()

    def setUp(self):
        self.client.force_login(self.user)

    def item_post_data(self, **overrides):
        """Every editable field on the PurchaseInvoiceItem admin form."""
        data = {
            "business": self.business.pk,
            "product": "",
            "quantity": "5",
            "track_code": "",
            "notes": "",
            "purchase_invoice": self.invoice.pk,
            "unit_cost": "",
            "quantity_received": "0",
        }
        data.update(overrides)
        return data

    # -- masking ----------------------------------------------------------

    def test_unit_cost_absent_from_change_form(self):
        response = self.client.get(
            reverse("admin:sales_purchaseinvoiceitem_change", args=[self.item.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, str(COST_CANARY))
        self.assertContains(response, MASK)

    def test_invoice_total_absent_from_fk_dropdown(self):
        """PurchaseInvoice.__str__ embeds total; the <select> must not."""
        response = self.client.get(reverse("admin:sales_purchaseinvoiceitem_add"))
        self.assertNotContains(response, str(TOTAL_CANARY))
        self.assertContains(response, MASK)

    def test_invoice_total_absent_from_changelist(self):
        """The changelist falls back to __str__ when list_display is unset."""
        response = self.client.get(reverse("admin:sales_purchaseinvoice_changelist"))
        self.assertNotContains(response, str(TOTAL_CANARY))
        self.assertContains(response, MASK)

    # -- editing ----------------------------------------------------------

    def test_blank_unit_cost_keeps_stored_value(self):
        response = self.client.post(
            reverse("admin:sales_purchaseinvoiceitem_change", args=[self.item.pk]),
            self.item_post_data(quantity="9"),
        )
        self.assertEqual(response.status_code, 302)

        self.item.refresh_from_db()
        self.assertEqual(self.item.unit_cost, COST_CANARY)
        self.assertEqual(self.item.quantity, 9)

    def test_typed_unit_cost_updates(self):
        response = self.client.post(
            reverse("admin:sales_purchaseinvoiceitem_change", args=[self.item.pk]),
            self.item_post_data(unit_cost="12.5"),
        )
        self.assertEqual(response.status_code, 302)

        self.item.refresh_from_db()
        self.assertEqual(self.item.unit_cost, 12.5)

    # -- the add/change split ---------------------------------------------

    def test_blank_unit_cost_on_add_is_a_form_error_not_a_crash(self):
        """unit_cost is NOT NULL with no default, and PurchaseInvoiceItem.save()
        calls full_clean(). Letting a blank through would raise inside
        save_model and render a 500 instead of a field error."""
        before = PurchaseInvoiceItem.objects.count()

        response = self.client.post(
            reverse("admin:sales_purchaseinvoiceitem_add"), self.item_post_data()
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This field is required")
        self.assertEqual(PurchaseInvoiceItem.objects.count(), before)

    def test_typed_unit_cost_on_add_creates_the_row(self):
        response = self.client.post(
            reverse("admin:sales_purchaseinvoiceitem_add"),
            self.item_post_data(unit_cost="45.75", quantity="2"),
        )
        self.assertEqual(response.status_code, 302)

        created = PurchaseInvoiceItem.objects.get(quantity=2)
        self.assertEqual(created.unit_cost, 45.75)


class CaptureWalkInCustomerTests(TestCase):
    """POST /sales-invoices/{id}/capture-customer/

    A counter sale is raised against the business's Walk-In Customer because
    the buyer is not known at the till. This endpoint names them when the
    invoice is printed: it creates the customer and reassigns the invoice in
    one transaction, so the client can never be left holding a customer that no
    invoice points at.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email="capture@example.com", password="pw-for-tests"
        )
        cls.city = City.objects.create(name="Karachi", postal_code="75000")
        # get_active_business() filters on is_active, so the flag matters here.
        cls.business = Business.objects.create(
            name="Counter Traders", owner=cls.user,
            phone="03001234567", is_active=True,
        )
        # Created by root.signals.createWalkInCustomer.
        cls.walk_in = Customer.objects.get(
            business=cls.business, name=WALK_IN_CUSTOMER_NAME
        )
        cls.invoice = SalesInvoice.objects.create(
            business=cls.business, customer=cls.walk_in,
            created_by=cls.user, status="C", invoice_number="INV-CAP-1",
            sub_total=1000, total=1000,
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def url(self, invoice=None):
        return f"/sales-invoices/{(invoice or self.invoice).pk}/capture-customer/"

    def payload(self, **overrides):
        data = {
            "name": "Ayesha Khan",
            "phone": "03001234567",
            "city": self.city.id,
            "email": "ayesha@example.com",
        }
        data.update(overrides)
        return data

    def test_creates_customer_and_reassigns_the_invoice(self):
        response = self.client.post(self.url(), self.payload(), format="json")
        self.assertEqual(response.status_code, 200, response.content)

        body = response.json()
        self.assertEqual(body["invoice"]["customer"]["name"], "Ayesha Khan")
        self.assertEqual(body["invoice"]["customer"]["email"], "ayesha@example.com")

        self.invoice.refresh_from_db()
        self.assertNotEqual(self.invoice.customer_id, self.walk_in.pk)
        self.assertEqual(self.invoice.customer.name, "Ayesha Khan")
        # The placeholder survives for the next counter sale.
        self.assertTrue(Customer.objects.filter(pk=self.walk_in.pk).exists())

    def test_returns_a_whatsapp_link_for_the_entered_number(self):
        response = self.client.post(self.url(), self.payload(), format="json")

        whatsapp = response.json()["whatsapp"]
        self.assertTrue(whatsapp["whatsapp_url"].startswith("https://wa.me/92"))
        self.assertIn("Ayesha Khan", whatsapp["message"])

    def test_email_is_optional(self):
        response = self.client.post(
            self.url(), self.payload(email=""), format="json"
        )
        self.assertEqual(response.status_code, 200, response.content)

    def test_missing_phone_is_rejected_and_writes_nothing(self):
        before = Customer.objects.count()

        response = self.client.post(
            self.url(), self.payload(phone=""), format="json"
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("phone", response.json())
        self.assertEqual(Customer.objects.count(), before)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.customer_id, self.walk_in.pk)

    def test_missing_city_is_rejected_and_writes_nothing(self):
        before = Customer.objects.count()

        payload = self.payload()
        payload.pop("city")
        response = self.client.post(self.url(), payload, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertIn("city", response.json())
        self.assertEqual(Customer.objects.count(), before)

    def test_another_businesss_invoice_is_not_reachable(self):
        other_user = User.objects.create_user(
            email="other@example.com", password="pw-for-tests"
        )
        other_business = Business.objects.create(
            name="Someone Else", owner=other_user,
            phone="03009999999", is_active=True,
        )
        other_invoice = SalesInvoice.objects.create(
            business=other_business,
            customer=Customer.objects.get(
                business=other_business, name=WALK_IN_CUSTOMER_NAME),
            created_by=other_user, status="C", total=500,
        )

        response = self.client.post(
            self.url(other_invoice), self.payload(), format="json"
        )

        self.assertEqual(response.status_code, 404)

    def test_capture_does_not_deduct_stock(self):
        """The regression test for using .update() instead of .save().

        updateInventoryOnSale fires on every SalesInvoice.save(), so going
        through the serializer would take a stock deduction pass as a side
        effect of renaming the buyer.
        """
        unit = Unit.objects.create(name="Piece", abv="pc")
        product = Product.objects.create(
            business=self.business, name="Cement Bag", unit=unit
        )
        variant = ProductVariant.objects.create(
            base=product, name="50kg", sku="CEM-50"
        )
        invoice = SalesInvoice.objects.create(
            business=self.business, customer=self.walk_in,
            created_by=self.user, status="C", invoice_number="INV-CAP-2",
        )
        SalesInvoiceItem.objects.create(
            business=self.business, sales_invoice=invoice,
            product=variant, quantity=5, unit_price=200,
        )
        invoice.refresh_from_db()
        self.assertFalse(invoice.is_deducted)

        response = self.client.post(
            f"/sales-invoices/{invoice.pk}/capture-customer/",
            self.payload(), format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)

        invoice.refresh_from_db()
        self.assertFalse(invoice.is_deducted)
        self.assertEqual(
            SalesInvoiceItemDeduction.objects.filter(sales_invoice=invoice).count(), 0
        )


class RepeatedProductOnSalesInvoiceTests(TestCase):
    """POST /sales-invoices/create-with-items/ with one product on several lines.

    A shop bills the same base product at different prices on one invoice -
    four paint colours off a single product, an old price beside a new one.
    That used to violate unique_together('sales_invoice', 'product') and came
    back as a 500 with no explanation (production invoice 1304). The key now
    includes unit_price, so different prices are separate lines and the same
    price is still refused as a double-entered line.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email="repeated@example.com", password="pw-for-tests"
        )
        cls.city = City.objects.create(name="Karachi", postal_code="75000")
        cls.business = Business.objects.create(
            name="Repeat Paints", owner=cls.user,
            phone="03001234567", is_active=True,
        )
        cls.customer = Customer.objects.get(
            business=cls.business, name=WALK_IN_CUSTOMER_NAME
        )
        cls.unit = Unit.objects.create(name="Pieces", abv="Pcs")
        cls.product = Product.objects.create(
            business=cls.business, name="Paint 1L", unit=cls.unit
        )
        cls.variant = ProductVariant.objects.create(
            base=cls.product, name="default", sku="RPT-1"
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def post(self, number, items):
        return self.client.post(
            "/sales-invoices/create-with-items/",
            {
                "invoice_number": number,
                "customer": str(self.customer.id),
                "date_issued": "2026-05-10",
                "date_due": "2026-05-10",
                "discount": {"value": 0, "type": "amount"},
                "tax": {"value": 0, "type": "percentage"},
                "project": None,
                "items": items,
            },
            format="json",
        )

    def line(self, price, quantity=1):
        return {"product_id": self.variant.id, "quantity": quantity,
                "unit_price": price}

    def test_one_product_at_four_prices_is_four_lines(self):
        """The exact shape of production invoice 1304."""
        response = self.post(
            "1304", [self.line(p) for p in (11271, 11458, 11556, 15457)]
        )
        self.assertEqual(response.status_code, 201, response.content)

        invoice = SalesInvoice.objects.get(
            business=self.business, invoice_number="1304"
        )
        self.assertEqual(invoice.invoice_items.count(), 4)
        self.assertEqual(
            sorted(item.unit_price for item in invoice.invoice_items.all()),
            [11271, 11458, 11556, 15457],
        )
        self.assertEqual(invoice.sub_total, 49742)

    def test_the_same_product_at_the_same_price_is_still_refused(self):
        """Two identical lines are one line entered twice; the invoice screen
        merges them into a single row's quantity rather than sending both.

        The view turns the constraint error into a 500, which is the shape it
        has always had for a failed write here. Nothing is left behind.
        """
        response = self.post("1306", [self.line(500), self.line(500)])
        self.assertEqual(response.status_code, 500)
        self.assertFalse(
            SalesInvoice.objects.filter(
                business=self.business, invoice_number="1306").exists()
        )

    def test_every_repeated_line_deducts_its_own_stock(self):
        """items_by_product() groups lines per product. While it returned one
        item per product, only the last line's quantity ever left stock."""
        inventory = Inventory.objects.get(business=self.business)
        stock = InventoryItem.objects.create(
            business=self.business, inventory=inventory, product=self.variant,
            quantity=100, quantity_on_hand=100, unit_cost=50,
        )

        response = self.post(
            "1307",
            [self.line(100, 2), self.line(200, 3), self.line(300, 5)],
        )
        self.assertEqual(response.status_code, 201, response.content)

        stock.refresh_from_db()
        self.assertEqual(stock.quantity, 90)   # 2 + 3 + 5 all left
        invoice = SalesInvoice.objects.get(
            business=self.business, invoice_number="1307"
        )
        self.assertEqual(invoice.total, 2 * 100 + 3 * 200 + 5 * 300)


class DerivedSalesInvoiceStatusTests(TestCase):
    """Status is derived from the invoice, not chosen - and deduction follows it.

    Bug #001: adding a line while updating an invoice flipped it to
    "Partially Completed" and it could never be set back. The cause was that
    `status` meant two things at once - the document state a person picked, and
    a record of how much stock had left - so `update_deduction_flags()` could
    overwrite the chosen value on any save.

    The rule now: no lines -> Draft; lines -> Completed, which deducts stock;
    lines plus a balance past the due date -> Overdue, produced on read and
    never stored. The only manual transition is Cancelled, which restocks.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email="derived-status@example.com", password="pw-for-tests"
        )
        City.objects.create(name="Lahore", postal_code="54000")
        cls.business = Business.objects.create(
            name="Derived Paints", owner=cls.user,
            phone="03001234567", is_active=True,
        )
        cls.customer = Customer.objects.get(
            business=cls.business, name=WALK_IN_CUSTOMER_NAME
        )
        cls.unit = Unit.objects.create(name="Litres", abv="L")
        cls.product = Product.objects.create(
            business=cls.business, name="Emulsion 4L", unit=cls.unit
        )
        cls.variant = ProductVariant.objects.create(
            base=cls.product, name="white", sku="DRV-1"
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    # --- helpers ---------------------------------------------------------

    def stock(self, quantity=100):
        return InventoryItem.objects.create(
            business=self.business,
            inventory=Inventory.objects.get(business=self.business),
            product=self.variant, quantity=quantity,
            quantity_on_hand=quantity, unit_cost=50,
        )

    def line(self, price=100, quantity=1):
        return {"product_id": self.variant.id, "quantity": quantity,
                "unit_price": price}

    def create_invoice(self, number, items, due=FAR_FUTURE):
        response = self.client.post(
            "/sales-invoices/create-with-items/",
            {
                "invoice_number": number,
                "customer": str(self.customer.id),
                "date_issued": "2026-05-10",
                "date_due": due,
                "discount": {"value": 0, "type": "amount"},
                "tax": {"value": 0, "type": "percentage"},
                "project": None,
                "items": items,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        return SalesInvoice.objects.get(
            business=self.business, invoice_number=number)

    def update_invoice(self, invoice, items, due=FAR_FUTURE):
        """The bundled update, with `items` in the shape the page sends:
        an existing line carries its id, a new line carries id=0."""
        return self.client.post(
            f"/sales-invoices/{invoice.pk}/update-with-items/",
            {
                "invoice_number": invoice.invoice_number,
                "customer": str(self.customer.id),
                "date_issued": "2026-05-10",
                "date_due": due,
                "discount": {"value": 0, "type": "amount"},
                "tax": {"value": 0, "type": "percentage"},
                "project": None,
                "items": items,
            },
            format="json",
        )

    def existing_lines(self, invoice):
        return [{"id": item.id, "product_id": item.product_id,
                 "quantity": item.quantity, "unit_price": item.unit_price}
                for item in invoice.invoice_items.all()]

    def deduction_count(self, invoice):
        return SalesInvoiceItemDeduction.objects.filter(
            sales_invoice=invoice).count()

    # --- the derived rule ------------------------------------------------

    def test_an_invoice_with_no_lines_is_a_draft(self):
        invoice = SalesInvoice.objects.create(
            business=self.business, customer=self.customer,
            created_by=self.user, invoice_number="DRV-EMPTY",
        )
        self.assertEqual(invoice.display_status, "D")
        self.assertEqual(invoice.stored_status, "D")

    def test_a_new_invoice_defaults_to_draft_not_cancelled(self):
        """The default used to be SALES_INVOICE_STATUS_CHOICES[2] - the whole
        tuple rather than a code. With SENT and PARTIALLY_COMPLETED gone,
        index 2 is now CANCELLED, which would have dropped every new invoice
        out of the party ledger.
        """
        invoice = SalesInvoice.objects.create(
            business=self.business, customer=self.customer,
            created_by=self.user, invoice_number="DRV-DEFAULT",
        )
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "D")

    def test_one_line_makes_it_completed(self):
        invoice = self.create_invoice("DRV-1", [self.line()])
        self.assertEqual(invoice.status, "C")
        self.assertEqual(invoice.display_status, "C")

    def test_removing_the_last_line_returns_it_to_draft(self):
        invoice = self.create_invoice("DRV-2", [self.line()])
        response = self.update_invoice(invoice, [])
        self.assertEqual(response.status_code, 200, response.content)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "D")

    def test_a_balance_past_the_due_date_reads_as_overdue(self):
        invoice = self.create_invoice("DRV-3", [self.line(1000)],
                                      due="2020-01-01")
        self.assertEqual(invoice.display_status, "O")
        # Derived on read only: the column still holds the stored value.
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "C")

    def test_overdue_is_never_written_to_the_column(self):
        invoice = self.create_invoice("DRV-4", [self.line(1000)],
                                      due="2020-01-01")
        invoice.recalculate_status()
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "C")

    def test_cancelled_is_sticky_through_an_item_change(self):
        """The one manual status. Nothing derived may recalculate it away."""
        invoice = self.create_invoice("DRV-5", [self.line()])
        self.client.post(f"/sales-invoices/{invoice.pk}/cancel/")
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "X")

        SalesInvoiceItem.objects.create(
            business=self.business, sales_invoice=invoice,
            product=self.variant, quantity=1, unit_price=500,
        )
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "X")
        self.assertEqual(invoice.display_status, "X")

    # --- the Bug #001 regression -----------------------------------------

    def test_adding_a_line_on_update_keeps_the_invoice_completed(self):
        """The reported bug, end to end.

        The update path creates items without `quantity_received`, which the
        old update_restock_flags() read - so the new line was flagged
        partially deducted forever, and that flag was written back onto the
        invoice as its status.
        """
        self.stock(100)
        invoice = self.create_invoice("DRV-6", [self.line(100, 2)])

        response = self.update_invoice(
            invoice,
            self.existing_lines(invoice) + [{"id": 0, **self.line(300, 5)}],
        )
        self.assertEqual(response.status_code, 200, response.content)

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "C")
        self.assertEqual(invoice.invoice_items.count(), 2)
        self.assertTrue(
            all(item.is_deducted for item in invoice.invoice_items.all()))

        # And it stays Completed on a second update. The old flag never
        # recovered, so the invoice could not be corrected back.
        response = self.update_invoice(invoice, self.existing_lines(invoice))
        self.assertEqual(response.status_code, 200, response.content)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "C")

    def test_a_line_added_on_update_leaves_stock(self):
        """The invoice-level is_deducted flag stays True once an invoice has
        been deducted, so guarding on it would mean a line added later never
        left stock.
        """
        stock = self.stock(100)
        invoice = self.create_invoice("DRV-7", [self.line(100, 2)])
        stock.refresh_from_db()
        self.assertEqual(stock.quantity, 98)

        self.update_invoice(
            invoice,
            self.existing_lines(invoice) + [{"id": 0, **self.line(300, 5)}],
        )
        stock.refresh_from_db()
        self.assertEqual(stock.quantity, 93)
        self.assertEqual(self.deduction_count(invoice), 2)

    # --- deduction and restocking ----------------------------------------

    def test_completing_deducts_exactly_once(self):
        stock = self.stock(100)
        invoice = self.create_invoice("DRV-8", [self.line(100, 4)])
        stock.refresh_from_db()
        self.assertEqual(stock.quantity, 96)
        self.assertEqual(self.deduction_count(invoice), 1)

        # A second run must not move stock again.
        updateInventoryOnSale(invoice)
        stock.refresh_from_db()
        self.assertEqual(stock.quantity, 96)
        self.assertEqual(self.deduction_count(invoice), 1)

    def test_cancelling_returns_the_stock(self):
        stock = self.stock(100)
        invoice = self.create_invoice("DRV-9", [self.line(100, 4)])

        response = self.client.post(f"/sales-invoices/{invoice.pk}/cancel/")
        self.assertEqual(response.status_code, 200, response.content)

        stock.refresh_from_db()
        self.assertEqual(stock.quantity, 100)
        self.assertEqual(self.deduction_count(invoice), 0)
        invoice.refresh_from_db()
        self.assertFalse(invoice.is_deducted)

    def test_uncancelling_deducts_again_exactly_once(self):
        stock = self.stock(100)
        invoice = self.create_invoice("DRV-10", [self.line(100, 4)])
        self.client.post(f"/sales-invoices/{invoice.pk}/cancel/")

        response = self.client.post(f"/sales-invoices/{invoice.pk}/uncancel/")
        self.assertEqual(response.status_code, 200, response.content)

        stock.refresh_from_db()
        self.assertEqual(stock.quantity, 96)
        self.assertEqual(self.deduction_count(invoice), 1)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "C")

    def test_a_cancelled_invoice_never_deducts(self):
        stock = self.stock(100)
        invoice = SalesInvoice.objects.create(
            business=self.business, customer=self.customer,
            created_by=self.user, invoice_number="DRV-11", status="X",
        )
        SalesInvoiceItem.objects.create(
            business=self.business, sales_invoice=invoice,
            product=self.variant, quantity=3, unit_price=100,
        )
        updateInventoryOnSale(invoice)

        stock.refresh_from_db()
        self.assertEqual(stock.quantity, 100)
        self.assertEqual(self.deduction_count(invoice), 0)

    def test_a_flagged_line_with_an_empty_ledger_is_left_alone(self):
        """How the migrated invoices are stored: is_deducted True with no
        deduction rows, precisely so that importing them moved no stock.
        Deducting on the delta alone would empty the shelves the first time
        one of them was edited.
        """
        stock = self.stock(100)
        invoice = SalesInvoice.objects.create(
            business=self.business, customer=self.customer,
            created_by=self.user, invoice_number="DRV-12",
        )
        item = SalesInvoiceItem.objects.create(
            business=self.business, sales_invoice=invoice,
            product=self.variant, quantity=9, unit_price=100,
        )
        SalesInvoiceItem.objects.filter(pk=item.pk).update(is_deducted=True)
        invoice.refresh_from_db()

        updateInventoryOnSale(invoice)

        stock.refresh_from_db()
        self.assertEqual(stock.quantity, 100)
        self.assertEqual(self.deduction_count(invoice), 0)

    # --- the list filter --------------------------------------------------

    def test_the_status_filter_separates_completed_from_overdue(self):
        """They share the stored value 'C', so an exact column match would
        return overdue invoices under Completed.
        """
        completed = self.create_invoice("DRV-13", [self.line(100)])
        overdue = self.create_invoice("DRV-14", [self.line(100)],
                                      due="2020-01-01")

        def ids(status_code):
            response = self.client.get(f"/sales-invoices/?status={status_code}")
            self.assertEqual(response.status_code, 200, response.content)
            body = response.json()
            rows = body["results"] if isinstance(body, dict) else body
            return {row["id"] for row in rows}

        self.assertIn(overdue.id, ids("O"))
        self.assertNotIn(completed.id, ids("O"))
        self.assertIn(completed.id, ids("C"))
        self.assertNotIn(overdue.id, ids("C"))
        self.assertEqual(ids("C") & ids("O"), set())

    def test_the_serializer_reports_the_derived_status(self):
        invoice = self.create_invoice("DRV-15", [self.line(1000)],
                                      due="2020-01-01")
        response = self.client.get(f"/sales-invoices/{invoice.pk}/")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], "O")

    def test_a_client_cannot_set_the_status(self):
        """It is derived, so the field is gone from every writable serializer
        and a sent value is ignored rather than honoured.
        """
        response = self.client.post(
            "/sales-invoices/create-with-items/",
            {
                "invoice_number": "DRV-16",
                "customer": str(self.customer.id),
                "date_issued": "2026-05-10",
                "date_due": FAR_FUTURE,
                "discount": {"value": 0, "type": "amount"},
                "tax": {"value": 0, "type": "percentage"},
                "status": "X",
                "project": None,
                "items": [self.line(100)],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        invoice = SalesInvoice.objects.get(
            business=self.business, invoice_number="DRV-16")
        self.assertEqual(invoice.status, "C")


class SalesInvoiceExtraChargesTests(TestCase):
    """Named charges added after tax, and out of revenue but not out of the bill.

    A shop bills PKR 500 for delivery on top of PKR 1000 of paint. The
    customer owes 1500 and the ledger says so, but only 1000 of it was a sale:
    the delivery fee is collected alongside the goods, not earned on them, and
    there is no cost of goods behind it. So `total` carries the charge and
    GOODS_TOTAL - revenue, profit and the dashboard sales figure - does not.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email="charges@example.com", password="pw-for-tests"
        )
        City.objects.create(name="Multan", postal_code="60000")
        cls.business = Business.objects.create(
            name="Charged Paints", owner=cls.user,
            phone="03001234567", is_active=True,
        )
        cls.customer = Customer.objects.get(
            business=cls.business, name=WALK_IN_CUSTOMER_NAME
        )
        cls.unit = Unit.objects.create(name="Gallons", abv="Gal")
        cls.product = Product.objects.create(
            business=cls.business, name="Enamel 1G", unit=cls.unit
        )
        cls.variant = ProductVariant.objects.create(
            base=cls.product, name="black", sku="CHG-1"
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def post(self, number, charges, tax=0, discount=0, quantity=1,
             price=1000, expect=201):
        response = self.client.post(
            "/sales-invoices/create-with-items/",
            {
                "invoice_number": number,
                "customer": str(self.customer.id),
                "date_issued": "2026-05-10",
                "date_due": FAR_FUTURE,
                "discount": {"value": discount, "type": "amount"},
                "tax": {"value": tax, "type": "percentage"},
                "extra_charges": charges,
                "project": None,
                "items": [{"product_id": self.variant.id,
                           "quantity": quantity, "unit_price": price}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, expect, response.content)
        if expect != 201:
            return response
        return SalesInvoice.objects.get(
            business=self.business, invoice_number=number)

    # --- the maths --------------------------------------------------------

    def test_a_charge_is_added_after_tax_and_is_not_itself_taxed(self):
        """Goods 1000, 10% tax, 100 discount, a 500 delivery charge.

        sub_total stays goods-only, so the tax is 100 and not 150: a delivery
        fee is a pass-through, and taxing it would overcharge the customer.
        Invoice.jsx recomputes a percentage tax from sub_total to print it, so
        the two agree only while the charges stay out of it.
        """
        invoice = self.post("CHG-1", [{"label": "Delivery", "amount": 500}],
                            tax=10, discount=100)
        self.assertEqual(invoice.sub_total, 1000)
        self.assertEqual(invoice.extra_charges_total, 500)
        self.assertEqual(invoice.total, 1000 + 100 - 100 + 500)

    def test_several_charges_are_totalled(self):
        invoice = self.post("CHG-2", [
            {"label": "Delivery", "amount": 500},
            {"label": "Labour", "amount": 250.5},
        ])
        self.assertEqual(invoice.extra_charges_total, 750.5)
        self.assertEqual(invoice.total, 1750.5)

    def test_two_charges_may_share_a_label(self):
        """Two deliveries on one invoice is an ordinary thing."""
        invoice = self.post("CHG-3", [
            {"label": "Delivery", "amount": 300},
            {"label": "Delivery", "amount": 200},
        ])
        self.assertEqual(invoice.extra_charges_total, 500)
        self.assertEqual(len(invoice.extra_charges), 2)

    def test_no_charges_leaves_the_total_as_it_was(self):
        invoice = self.post("CHG-4", [])
        self.assertEqual(invoice.extra_charges_total, 0)
        self.assertEqual(invoice.total, 1000)

    def test_a_charge_survives_an_item_being_added(self):
        """adjust_totals runs again from the item signal, and it rewrites both
        the total and the denormalised charges figure from the same source."""
        invoice = self.post("CHG-5", [{"label": "Delivery", "amount": 500}])
        SalesInvoiceItem.objects.create(
            business=self.business, sales_invoice=invoice,
            product=self.variant, quantity=1, unit_price=250,
        )
        invoice.refresh_from_db()
        self.assertEqual(invoice.sub_total, 1250)
        self.assertEqual(invoice.extra_charges_total, 500)
        self.assertEqual(invoice.total, 1750)

    # --- what the charge is and is not part of ----------------------------

    def test_revenue_excludes_the_charge_but_the_bill_includes_it(self):
        """The whole decision, in one test."""
        invoice = self.post("CHG-6", [{"label": "Delivery", "amount": 500}])

        self.assertEqual(invoice.total, 1500)
        self.assertEqual(
            SalesInvoice.objects.revenue(
                self.business.id, date(2026, 5, 1), date(2026, 5, 31)),
            1000,
        )
        self.assertEqual(
            SalesInvoice.objects.total_sales(self.business.id), 1000)
        self.assertEqual(
            profit_estimate(self.business.id,
                            date(2026, 5, 1), date(2026, 5, 31))['revenue'],
            1000,
        )
        # The customer genuinely owes the delivery fee, so everything that
        # answers "what is owed" counts it.
        self.assertEqual(
            party_invoiced(self.business.id, customer=self.customer),
            1500,
        )
        self.assertEqual(invoice_room(invoice), 1500)

    def test_the_charge_counts_towards_being_paid_in_full(self):
        invoice = self.post("CHG-7", [{"label": "Delivery", "amount": 500}])
        SalesReceipt.objects.create(sales_invoice=invoice, amount=1000)
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, "PP")

        SalesReceipt.objects.create(sales_invoice=invoice, amount=500)
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, "P")

    def test_the_average_order_value_keeps_the_charge(self):
        """Unlike revenue: this is the average bill a customer was handed, and
        the delivery fee was on it."""
        self.post("CHG-8", [{"label": "Delivery", "amount": 500}])
        self.assertEqual(
            SalesInvoice.objects.average_order_value(self.business.id), 1500)

    def test_the_monthly_trend_excludes_the_charge(self):
        """monthly_trend is given an expression rather than a field name."""
        self.post("CHG-9", [{"label": "Delivery", "amount": 500}])
        trend = SalesInvoice.objects.monthly_sales_trend(self.business.id)
        self.assertEqual(sum(day["value"] for day in trend), 1000)

    def test_a_cancelled_charged_invoice_is_in_no_figure_at_all(self):
        invoice = self.post("CHG-10", [{"label": "Delivery", "amount": 500}])
        self.client.post(f"/sales-invoices/{invoice.pk}/cancel/")
        self.assertEqual(
            SalesInvoice.objects.revenue(
                self.business.id, date(2026, 5, 1), date(2026, 5, 31)),
            0,
        )

    def test_an_invoice_with_no_charges_column_set_still_aggregates(self):
        """GOODS_TOTAL subtracts a column, so a row that predates it must not
        drop out of the sum. The migration defaults it to 0.0 rather than NULL
        for exactly this reason."""
        invoice = self.post("CHG-11", [])
        SalesInvoice.objects.filter(pk=invoice.pk).update(extra_charges=None)
        self.assertEqual(
            SalesInvoice.objects.total_sales(self.business.id), 1000)

    # --- validation -------------------------------------------------------

    def test_a_negative_charge_is_refused(self):
        """It would be a discount - and because charges are subtracted from
        revenue, a negative one would add to it: money in the sales figures
        that nobody was ever billed."""
        self.post("CHG-12", [{"label": "Rebate", "amount": -500}], expect=400)

    def test_a_blank_label_is_refused(self):
        self.post("CHG-13", [{"label": "   ", "amount": 500}], expect=400)

    def test_an_unknown_key_is_refused(self):
        """A typo'd "ammount" would print as a charge and count as zero."""
        self.post("CHG-14", [{"label": "Delivery", "ammount": 500}],
                  expect=400)

    def test_an_object_instead_of_a_list_is_refused(self):
        self.post("CHG-15", {"label": "Delivery", "amount": 500}, expect=400)

    def test_a_non_numeric_amount_is_refused(self):
        self.post("CHG-16", [{"label": "Delivery", "amount": "lots"}],
                  expect=400)

    def test_an_over_long_label_is_refused(self):
        self.post("CHG-17", [{"label": "D" * 65, "amount": 500}], expect=400)

    def test_too_many_charges_are_refused(self):
        self.post(
            "CHG-18",
            [{"label": f"Charge {n}", "amount": 10} for n in range(21)],
            expect=400,
        )

    def test_a_zero_charge_is_dropped_rather_than_refused(self):
        """An empty row left on the form is a slip, not an error."""
        invoice = self.post("CHG-19", [
            {"label": "Delivery", "amount": 500},
            {"label": "Labour", "amount": 0},
        ])
        self.assertEqual(len(invoice.extra_charges), 1)
        self.assertEqual(invoice.extra_charges_total, 500)

    def test_an_amount_is_rounded_and_a_label_stripped(self):
        invoice = self.post("CHG-20", [{"label": "  Delivery  ",
                                        "amount": 500.456}])
        self.assertEqual(invoice.extra_charges,
                         [{"label": "Delivery", "amount": 500.46}])

    def test_a_client_sent_total_is_ignored(self):
        """extra_charges_total is derived. It is the figure every revenue and
        profit aggregate subtracts, so a client must not be able to set it."""
        response = self.client.post(
            "/sales-invoices/create-with-items/",
            {
                "invoice_number": "CHG-21",
                "customer": str(self.customer.id),
                "date_issued": "2026-05-10",
                "date_due": FAR_FUTURE,
                "discount": {"value": 0, "type": "amount"},
                "tax": {"value": 0, "type": "percentage"},
                "extra_charges": [{"label": "Delivery", "amount": 500}],
                "extra_charges_total": 999999,
                "project": None,
                "items": [{"product_id": self.variant.id,
                           "quantity": 1, "unit_price": 1000}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        invoice = SalesInvoice.objects.get(
            business=self.business, invoice_number="CHG-21")
        self.assertEqual(invoice.extra_charges_total, 500)

    # --- the surfaces -----------------------------------------------------

    def test_the_charges_reach_the_pdf_payload(self):
        """Without these two fields on GenerateInvoiceSerializer the printed
        invoice cannot show a charge the customer is being billed for."""
        invoice = self.post("CHG-22", [{"label": "Delivery", "amount": 500}])
        response = self.client.get(f"/sales-invoices/{invoice.pk}/print-invoice/")
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["extra_charges"],
                         [{"label": "Delivery", "amount": 500.0}])
        self.assertEqual(body["extra_charges_total"], 500.0)
        self.assertEqual(body["sub_total"], 1000)

    def test_the_charges_reach_the_whatsapp_message(self):
        invoice = self.post("CHG-23", [{"label": "Delivery", "amount": 500}])
        self.customer.phone = "03001234567"
        self.customer.save(update_fields=["phone"])
        invoice.refresh_from_db()

        payload = build_whatsapp_payload(invoice)
        self.assertIsNotNone(payload)
        self.assertIn("Delivery: PKR 500", payload["message"])
        self.assertIn("Total: PKR 1,500", payload["message"])


# Writes go to a temporary directory on disk, never to the configured store.
#
# MEDIA_ROOT on its own is not enough: USE_S3 switches STORAGES['default'] to
# S3Storage independently of DEBUG, and a test run that leaves it alone uploads
# its fixtures to the real bucket. STORAGES has to be overridden with it.
local_files = override_settings(
    MEDIA_ROOT=tempfile.mkdtemp(),
    STORAGES={
        'default': {
            'BACKEND': 'django.core.files.storage.FileSystemStorage',
        },
        'staticfiles': {
            'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage',
        },
    },
)


def a_png(name="slip.png"):
    """A real 1x1 PNG, as an upload.

    Encoded here rather than pasted as a base64 blob: ImageField validates by
    opening the file, so a file that merely ends in .png is rejected, and
    Pillow - which ImageField needs anyway - is the only thing that can be
    trusted to produce one it accepts.
    """
    buffer = BytesIO()
    Image.new("RGB", (1, 1), "white").save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(),
                              content_type="image/png")


@local_files
class InvoiceImageTests(TestCase):
    """Several photos per invoice: the delivery note, the goods, the bill.

    See local_files above for why the storage backend is overridden and not
    just MEDIA_ROOT.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email="invoice-images@example.com", password="pw-for-tests"
        )
        City.objects.create(name="Quetta", postal_code="87300")
        cls.business = Business.objects.create(
            name="Imaged Paints", owner=cls.user,
            phone="03001234567", is_active=True,
        )
        cls.customer = Customer.objects.get(
            business=cls.business, name=WALK_IN_CUSTOMER_NAME
        )
        cls.supplier = Supplier.objects.create(
            business=cls.business, name="Imaged Supply Co"
        )
        cls.invoice = SalesInvoice.objects.create(
            business=cls.business, customer=cls.customer,
            created_by=cls.user, invoice_number="IMG-1",
        )
        cls.purchase = PurchaseInvoice.objects.create(
            business=cls.business, supplier=cls.supplier,
            created_by=cls.user, status="D", invoice_number="PIMG-1",
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def url(self, invoice=None):
        return f"/sales-invoices/{(invoice or self.invoice).pk}/images/"

    def purchase_url(self, invoice=None):
        return f"/purchase-invoices/{(invoice or self.purchase).pk}/images/"

    # --- uploading --------------------------------------------------------

    def test_an_invoice_takes_several_images(self):
        for name in ("note.png", "goods.png", "bill.png"):
            response = self.client.post(
                self.url(), {"image": a_png(name)}, format="multipart")
            self.assertEqual(response.status_code, 201, response.content)

        self.assertEqual(self.invoice.images.count(), 3)

    def test_the_upload_records_who_did_it_and_the_invoice(self):
        response = self.client.post(
            self.url(), {"image": a_png(), "caption": "Signed delivery note"},
            format="multipart")
        self.assertEqual(response.status_code, 201, response.content)

        image = InvoiceImage.objects.get(id=response.json()["id"])
        self.assertEqual(image.sales_invoice_id, self.invoice.id)
        self.assertIsNone(image.purchase_invoice_id)
        self.assertEqual(image.uploaded_by_id, self.user.id)
        self.assertEqual(image.caption, "Signed delivery note")

    def test_a_purchase_invoice_takes_images_on_its_own_route(self):
        response = self.client.post(
            self.purchase_url(), {"image": a_png()}, format="multipart")
        self.assertEqual(response.status_code, 201, response.content)

        image = InvoiceImage.objects.get(id=response.json()["id"])
        self.assertEqual(image.purchase_invoice_id, self.purchase.id)
        self.assertIsNone(image.sales_invoice_id)

    def test_a_file_that_is_not_an_image_is_refused(self):
        response = self.client.post(
            self.url(),
            {"image": SimpleUploadedFile(
                "notes.png", b"this is not a png", content_type="image/png")},
            format="multipart",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(self.invoice.images.count(), 0)

    def test_an_invoice_id_in_the_payload_is_ignored(self):
        """Which invoice an image belongs to comes from the URL. Taking it from
        the body would be a way to attach a photo to another business's
        invoice."""
        other_user = User.objects.create_user(
            email="other-images@example.com", password="pw-for-tests")
        other_business = Business.objects.create(
            name="Someone Else", owner=other_user,
            phone="03009999999", is_active=True)
        other_invoice = SalesInvoice.objects.create(
            business=other_business,
            customer=Customer.objects.get(
                business=other_business, name=WALK_IN_CUSTOMER_NAME),
            created_by=other_user, invoice_number="OTHER-1")

        response = self.client.post(
            self.url(),
            {"image": a_png(), "sales_invoice": other_invoice.id},
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(
            InvoiceImage.objects.get(id=response.json()["id"]).sales_invoice_id,
            self.invoice.id,
        )

    # --- reading ----------------------------------------------------------

    def test_the_url_comes_back_absolute(self):
        """The page is served from another origin, so a bare /media/... path
        cannot be loaded. request has to be in the serializer context."""
        response = self.client.post(
            self.url(), {"image": a_png()}, format="multipart")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(response.json()["image"].startswith("http"),
                        response.json()["image"])

    def test_the_images_are_listed_on_the_invoice_detail(self):
        self.client.post(self.url(), {"image": a_png()}, format="multipart")
        response = self.client.get(f"/sales-invoices/{self.invoice.pk}/")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(response.json()["images"]), 1)

    def test_newest_first(self):
        first = self.client.post(
            self.url(), {"image": a_png("first.png")}, format="multipart")
        second = self.client.post(
            self.url(), {"image": a_png("second.png")}, format="multipart")

        listed = self.client.get(self.url()).json()
        rows = listed["results"] if isinstance(listed, dict) else listed
        self.assertEqual(
            [row["id"] for row in rows],
            [second.json()["id"], first.json()["id"]],
        )

    def test_another_businesss_invoice_has_no_reachable_images(self):
        other_user = User.objects.create_user(
            email="other-images-2@example.com", password="pw-for-tests")
        other_business = Business.objects.create(
            name="Someone Else Again", owner=other_user,
            phone="03008888888", is_active=True)
        other_invoice = SalesInvoice.objects.create(
            business=other_business,
            customer=Customer.objects.get(
                business=other_business, name=WALK_IN_CUSTOMER_NAME),
            created_by=other_user, invoice_number="OTHER-2")

        response = self.client.post(
            self.url(other_invoice), {"image": a_png()}, format="multipart")
        self.assertEqual(response.status_code, 404, response.content)

        listed = self.client.get(self.url(other_invoice)).json()
        rows = listed["results"] if isinstance(listed, dict) else listed
        self.assertEqual(rows, [])

    # --- deleting ---------------------------------------------------------

    def test_deleting_an_image_removes_the_row_and_the_file(self):
        created = self.client.post(
            self.url(), {"image": a_png()}, format="multipart").json()
        image = InvoiceImage.objects.get(id=created["id"])
        path = image.image.path
        self.assertTrue(os.path.exists(path))

        response = self.client.delete(f"{self.url()}{created['id']}/")
        self.assertEqual(response.status_code, 204, response.content)
        self.assertFalse(InvoiceImage.objects.filter(id=created["id"]).exists())
        # Django stopped deleting files with the row in 1.3, so an untouched
        # default would leave this behind for ever.
        self.assertFalse(os.path.exists(path))

    def test_deleting_the_invoice_takes_its_images_with_it(self):
        self.client.post(self.url(), {"image": a_png()}, format="multipart")
        invoice_id = self.invoice.id
        SalesInvoice.objects.filter(id=invoice_id).delete()
        self.assertEqual(
            InvoiceImage.objects.filter(sales_invoice_id=invoice_id).count(), 0)

    # --- the model's own rule ---------------------------------------------

    def test_an_image_cannot_belong_to_both_invoices(self):
        with self.assertRaises(Exception):
            InvoiceImage.objects.create(
                sales_invoice=self.invoice, purchase_invoice=self.purchase,
                image="invoices/whatever.png")

    def test_an_image_cannot_belong_to_neither(self):
        with self.assertRaises(Exception):
            InvoiceImage.objects.create(image="invoices/whatever.png")


@local_files
class PaymentImageTests(TestCase):
    """One photo per payment, on the transaction the receipt is mirrored into.

    No new column and no new table: a receipt is already mirrored 1:1 into a
    Transaction, and Transaction has carried an image since the accounting
    module landed.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email="payment-images@example.com", password="pw-for-tests"
        )
        City.objects.create(name="Sialkot", postal_code="51310")
        cls.business = Business.objects.create(
            name="Paid Paints", owner=cls.user,
            phone="03001234567", is_active=True,
        )
        cls.customer = Customer.objects.get(
            business=cls.business, name=WALK_IN_CUSTOMER_NAME
        )
        cls.invoice = SalesInvoice.objects.create(
            business=cls.business, customer=cls.customer,
            created_by=cls.user, invoice_number="PAY-IMG-1", total=5000,
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def url(self):
        return f"/sales-invoices/{self.invoice.pk}/payments/"

    def test_a_payment_can_carry_a_photo(self):
        response = self.client.post(
            self.url(),
            {"amount": "1000", "desc": "Cash", "image": a_png("cash.png")},
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.content)

        receipt = SalesReceipt.objects.get(sales_invoice=self.invoice)
        self.assertTrue(receipt.transaction_record.image)

    def test_the_photo_comes_back_absolute_on_the_payments_list(self):
        self.client.post(
            self.url(),
            {"amount": "1000", "image": a_png("cash.png")},
            format="multipart",
        )
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        rows = body["results"] if isinstance(body, dict) else body
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["image"].startswith("http"), rows[0]["image"])

    def test_a_payment_without_a_photo_reports_none(self):
        response = self.client.post(
            self.url(), {"amount": 1000}, format="json")
        self.assertEqual(response.status_code, 201, response.content)

        body = self.client.get(self.url()).json()
        rows = body["results"] if isinstance(body, dict) else body
        self.assertIsNone(rows[0]["image"])

    def test_a_payment_still_posts_as_plain_json(self):
        """The dialog only switches to multipart when a file is attached, so
        the numbers stay typed in the ordinary case."""
        response = self.client.post(
            self.url(), {"amount": 1500, "desc": "Bank"}, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(
            SalesReceipt.objects.get(sales_invoice=self.invoice).amount, 1500)
