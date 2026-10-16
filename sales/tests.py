from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from rest_framework.test import APIClient

from core.admin_masking import MASK
from root.models import (
    WALK_IN_CUSTOMER_NAME, Business, City, Customer, Product,
    ProductVariant, Supplier, Unit,
)

from inventory.models import Inventory, InventoryItem
from .models import (
    PurchaseInvoice, PurchaseInvoiceItem, SalesInvoice, SalesInvoiceItem,
    SalesInvoiceItemDeduction,
)
from .utils import updateInventoryOnSale

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


class PurchaseInvoiceTotalAfterUpdateTests(TestCase):
    """Updating a purchase invoice's lines updates the total it stores.

    The viewset fetches invoices with their items prefetched, and
    adjust_totals used to read those cached items - the list from before the
    update. A line added on the update screen showed there (the page adds up
    in the browser), but the stored total, which the list, the khaata and
    payables read, stayed at the old figure.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email="purchase-totals@example.com", password="pw-for-tests"
        )
        cls.business = Business.objects.create(
            name="Totalled Paints", owner=cls.user,
            phone="03001234567", is_active=True,
        )
        cls.supplier = Supplier.objects.create(
            business=cls.business, name="Totals Supply Co"
        )
        cls.unit = Unit.objects.create(name="Tins", abv="Tin")
        cls.product = Product.objects.create(
            business=cls.business, name="Thinner 1L", unit=cls.unit
        )
        cls.variant = ProductVariant.objects.create(
            base=cls.product, name="clear", sku="TOT-1"
        )
        cls.other = ProductVariant.objects.create(
            base=cls.product, name="strong", sku="TOT-2"
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def payload(self, items, tax=18):
        # A draft, so receiving the goods (and the stock logic that comes
        # with it) stays out of a test about the total.
        return {
            "invoice_number": "PUR-TOT-1",
            "supplier": self.supplier.id,
            "date_issued": "2026-05-10T12:00:00",
            "date_due": FAR_FUTURE,
            "delivery": FAR_FUTURE,
            "status": "D",
            "notes": "",
            "tax": {"value": tax, "type": "percentage"},
            "project": None,
            "items": items,
        }

    def test_adding_a_line_on_update_updates_the_stored_total(self):
        response = self.client.post(
            "/purchase-invoices/create-with-items/",
            self.payload([{"product_id": self.variant.id, "quantity": 5,
                           "unit_cost": 5000}]),
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        invoice = PurchaseInvoice.objects.get(
            business=self.business, invoice_number="PUR-TOT-1")
        self.assertEqual(invoice.total, 25000 * 1.18)

        line = invoice.invoice_items.get()
        response = self.client.post(
            f"/purchase-invoices/{invoice.pk}/update-with-items/",
            self.payload([
                {"id": line.id, "product_id": self.variant.id,
                 "quantity": 5, "unit_cost": 5000},
                {"id": None, "product_id": self.other.id,
                 "quantity": 10, "unit_cost": 5000},
            ]),
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)

        invoice.refresh_from_db()
        self.assertEqual(invoice.sub_total, 75000)
        self.assertEqual(invoice.total, 75000 * 1.18)

        # And the list - what the user was looking at - says the same.
        listed = self.client.get("/purchase-invoices/").json()
        rows = listed["results"] if isinstance(listed, dict) else listed
        row = next(r for r in rows if r["id"] == invoice.id)
        self.assertEqual(row["total"], 75000 * 1.18)

    def test_removing_a_line_on_update_updates_the_stored_total(self):
        response = self.client.post(
            "/purchase-invoices/create-with-items/",
            self.payload([
                {"product_id": self.variant.id, "quantity": 5, "unit_cost": 5000},
                {"product_id": self.other.id, "quantity": 10, "unit_cost": 5000},
            ], tax=0),
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        invoice = PurchaseInvoice.objects.get(
            business=self.business, invoice_number="PUR-TOT-1")
        self.assertEqual(invoice.total, 75000)

        keep = invoice.invoice_items.get(product=self.variant)
        response = self.client.post(
            f"/purchase-invoices/{invoice.pk}/update-with-items/",
            self.payload([{"id": keep.id, "product_id": self.variant.id,
                           "quantity": 5, "unit_cost": 5000}], tax=0),
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        invoice.refresh_from_db()
        self.assertEqual(invoice.total, 25000)
