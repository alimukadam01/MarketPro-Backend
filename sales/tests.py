from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from rest_framework.test import APIClient

from core.admin_masking import MASK
from root.models import (
    WALK_IN_CUSTOMER_NAME, Business, City, Customer, Product,
    ProductVariant, Supplier, Unit,
)

from .models import (
    PurchaseInvoice, PurchaseInvoiceItem, SalesInvoice, SalesInvoiceItem,
    SalesInvoiceItemDeduction,
)

User = get_user_model()

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
            created_by=cls.user, status="S", invoice_number="INV-CAP-1",
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
            created_by=other_user, status="S", total=500,
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
