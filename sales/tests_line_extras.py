"""Invoice lines: product extra fields (part 01) and line discounts (part 02).

plans/product-extra-fields.md. The case in hand: Urban Paints mixes paint in
the shop, so a quarter of Fenomastic in colour 1453 and a gallon in 5674 are
the same two variants as always - only the colour code differs.
"""
import importlib
from datetime import date

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models import ProtectedError
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework import serializers
from rest_framework.test import APIClient

from accounts.utils import profit_estimate
from inventory.models import Inventory, InventoryItem
from root.models import (
    WALK_IN_CUSTOMER_NAME, Business, City, Customer, Product, ProductField,
    ProductFieldLink, ProductVariant, Supplier, Unit,
)
from .lines import (
    clean_extra_fields, clean_line_discount, discount_off, line_total,
)
from .models import (
    PurchaseInvoice, PurchaseInvoiceItem, SalesInvoice, SalesInvoiceItem,
    SalesInvoiceItemDeduction,
)

User = get_user_model()
FAR_FUTURE = "2099-12-31"


class LineFixture(TestCase):
    """A paint shop with a colour code asked on sales, and a batch number
    asked on purchases."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="lines@example.com", password="pw-for-tests")
        City.objects.get_or_create(name="Karachi", defaults={"postal_code": "74000"})
        cls.business = Business.objects.create(
            name="Urban Paints", owner=cls.user, phone="03001234567", is_active=True)
        cls.customer = Customer.objects.get(business=cls.business, name=WALK_IN_CUSTOMER_NAME)
        cls.supplier = Supplier.objects.create(business=cls.business, name="Dealer")
        cls.unit = Unit.objects.create(name="Tins", abv="Tin")
        cls.paint = Product.objects.create(
            business=cls.business, name="Fenomastic Rich Matt A", unit=cls.unit)
        cls.quarter = ProductVariant.objects.create(base=cls.paint, name="Quarter", sku="FEN-Q")
        cls.gallon = ProductVariant.objects.create(base=cls.paint, name="Gallon", sku="FEN-G")
        cls.plain = Product.objects.create(business=cls.business, name="Brush", unit=cls.unit)
        cls.brush = ProductVariant.objects.create(base=cls.plain, name="default", sku="BR-1")

        cls.colour = ProductField.objects.create(business=cls.business, label="Colour code")
        cls.batch = ProductField.objects.create(
            business=cls.business, label="Batch no.", field_type=ProductField.NUMBER)
        ProductFieldLink.objects.create(
            product=cls.paint, field=cls.colour, applies_to=ProductFieldLink.SALE, required=True)
        ProductFieldLink.objects.create(
            product=cls.paint, field=cls.batch, applies_to=ProductFieldLink.PURCHASE)

        cls.other_business = Business.objects.create(
            name="Elsewhere", owner=User.objects.create_user(
                email="elsewhere@example.com", password="pw-for-tests"),
            phone="03001234568", is_active=True)
        cls.foreign_field = ProductField.objects.create(
            business=cls.other_business, label="Shade")

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        inventory = Inventory.objects.get(business=self.business)
        for variant in (self.quarter, self.gallon, self.brush):
            InventoryItem.objects.create(
                business=self.business, inventory=inventory, product=variant,
                quantity=100, quantity_on_hand=100, unit_cost=50, unit_price=100)

    def colour_of(self, code):
        return {str(self.colour.id): code}

    def sale(self, items, number="SAL-1", discount=None, tax=None, charges=None):
        response = self.client.post("/sales-invoices/create-with-items/", {
            "invoice_number": number,
            "customer": str(self.customer.id),
            "date_issued": "2026-11-09",
            "date_due": FAR_FUTURE,
            "discount": discount or {"value": 0, "type": "amount"},
            "tax": tax or {"value": 0, "type": "percentage"},
            "extra_charges": charges or [],
            "project": None,
            "items": items,
        }, format="json")
        return response

    def purchase(self, items, number="PUR-1", status="D"):
        return self.client.post("/purchase-invoices/create-with-items/", {
            "invoice_number": number,
            "supplier": self.supplier.id,
            "date_issued": "2026-11-09T12:00:00",
            "date_due": FAR_FUTURE,
            "delivery": "2026-11-09",
            "status": status,
            "notes": "",
            "tax": {"value": 0, "type": "percentage"},
            "project": None,
            "items": items,
        }, format="json")


# ── part 01: the rules ──────────────────────────────────────────────────────

class CleanExtraFieldsTests(LineFixture):

    def test_canonical_form(self):
        cleaned = clean_extra_fields(self.gallon, {str(self.colour.id): "  5674 "}, "sale")
        self.assertEqual(cleaned, {str(self.colour.id): "5674"})

    def test_number_field_is_a_number_and_key_order_is_fixed(self):
        cleaned = clean_extra_fields(self.gallon, {str(self.batch.id): "12.0"}, "purchase")
        self.assertEqual(cleaned, {str(self.batch.id): 12})
        with self.assertRaisesMessage(serializers.ValidationError, "Batch no. must be a number"):
            clean_extra_fields(self.gallon, {str(self.batch.id): "abc"}, "purchase")

    def test_a_field_asked_on_one_side_only(self):
        # The colour code is asked on sales; a purchase line may not answer it.
        with self.assertRaisesMessage(serializers.ValidationError, "not asked"):
            clean_extra_fields(self.gallon, self.colour_of("1453"), "purchase")
        # And not being required on purchases, none is fine.
        self.assertEqual(clean_extra_fields(self.gallon, {}, "purchase"), {})

    def test_required_missing_or_blank(self):
        for answers in ({}, None, self.colour_of("  ")):
            with self.subTest(answers=answers):
                with self.assertRaisesMessage(serializers.ValidationError, "Enter the colour code"):
                    clean_extra_fields(self.gallon, answers, "sale")

    def test_unknown_field_and_too_long(self):
        with self.assertRaisesMessage(serializers.ValidationError, "not asked"):
            clean_extra_fields(self.brush, self.colour_of("1453"), "sale")
        with self.assertRaisesMessage(serializers.ValidationError, "at most 100"):
            clean_extra_fields(self.gallon, self.colour_of("x" * 101), "sale")

    def test_a_retired_field_is_no_longer_asked(self):
        self.colour.is_active = False
        self.colour.save()
        self.assertEqual(clean_extra_fields(self.gallon, {}, "sale"), {})


# ── part 01: the Urban Paints case ──────────────────────────────────────────

class ColourCodeOnSalesTests(LineFixture):

    def test_two_colours_of_one_gallon_are_two_lines(self):
        response = self.sale([
            {"product_id": self.quarter.id, "quantity": 1, "unit_price": 1150,
             "extra_fields": self.colour_of("1453")},
            {"product_id": self.gallon.id, "quantity": 2, "unit_price": 4200,
             "extra_fields": self.colour_of("5674")},
            {"product_id": self.gallon.id, "quantity": 1, "unit_price": 4200,
             "extra_fields": self.colour_of("1453")},
        ])
        self.assertEqual(response.status_code, 201, response.content)
        invoice = SalesInvoice.objects.get(business=self.business, invoice_number="SAL-1")
        lines = list(invoice.invoice_items.order_by("id"))
        self.assertEqual([line.extra_fields for line in lines],
                         [self.colour_of("1453"), self.colour_of("5674"), self.colour_of("1453")])
        self.assertEqual(invoice.total, 1150 + 8400 + 4200)

        # Stock is untouched by the colour: each variant goes down by all of it,
        # one deduction per line.
        self.assertEqual(InventoryItem.objects.get(product=self.gallon).quantity_on_hand, 97)
        self.assertEqual(InventoryItem.objects.get(product=self.quarter).quantity_on_hand, 99)
        self.assertEqual(SalesInvoiceItemDeduction.objects.filter(sales_invoice=invoice).count(), 3)

    def test_the_same_line_twice_is_a_400_not_a_500(self):
        line = {"product_id": self.gallon.id, "quantity": 1, "unit_price": 4200,
                "extra_fields": self.colour_of("5674")}
        response = self.sale([line, dict(line)])
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("Lines 1 and 2", str(response.content))

    def test_missing_colour_is_refused_with_the_line_number(self):
        response = self.sale([
            {"product_id": self.brush.id, "quantity": 1, "unit_price": 100},
            {"product_id": self.gallon.id, "quantity": 1, "unit_price": 4200},
        ])
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("Line 2: Enter the colour code", str(response.content))

    def test_reading_shows_the_current_name(self):
        self.sale([{"product_id": self.gallon.id, "quantity": 1, "unit_price": 4200,
                    "extra_fields": self.colour_of("5674")}])
        self.colour.label = "Shade"
        self.colour.save()
        invoice = SalesInvoice.objects.get(business=self.business)
        detail = self.client.get(f"/sales-invoices/{invoice.id}/").json()
        line = detail["invoice_items"][0]
        self.assertEqual(line["extra_fields_display"], [{"label": "Shade", "value": "5674"}])

    def test_update_keeps_a_saved_lines_answers(self):
        self.sale([{"product_id": self.gallon.id, "quantity": 1, "unit_price": 4200,
                    "extra_fields": self.colour_of("5674")}])
        invoice = SalesInvoice.objects.get(business=self.business)
        line = invoice.invoice_items.get()
        # A field added to the product later, required, does not stop the old
        # invoice saving: its saved line is not asked again.
        size = ProductField.objects.create(business=self.business, label="Sheen")
        ProductFieldLink.objects.create(product=self.paint, field=size, required=True)
        response = self.client.post(f"/sales-invoices/{invoice.id}/update-with-items/", {
            "invoice_number": "SAL-1", "customer": str(self.customer.id),
            "date_issued": "2026-11-09", "date_due": FAR_FUTURE,
            "discount": {"value": 0, "type": "amount"}, "tax": {"value": 0, "type": "percentage"},
            "project": None,
            "items": [{"id": line.id, "product_id": self.gallon.id, "quantity": 1,
                       "unit_price": 4200, "extra_fields": self.colour_of("9999")}],
        }, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        line.refresh_from_db()
        self.assertEqual(line.extra_fields, self.colour_of("5674"))

    def test_search_finds_the_colour(self):
        self.sale([{"product_id": self.gallon.id, "quantity": 1, "unit_price": 4200,
                    "extra_fields": self.colour_of("5674")}])
        self.sale([{"product_id": self.gallon.id, "quantity": 1, "unit_price": 4200,
                    "extra_fields": self.colour_of("1453")}], number="SAL-2")
        listed = self.client.get("/sales-invoices/", {"search": "5674"}).json()
        rows = listed["results"] if isinstance(listed, dict) else listed
        self.assertEqual([row["invoice_number"] for row in rows], ["SAL-1"])

    def test_reading_lines_adds_no_query_per_line(self):
        def count(lines):
            SalesInvoice.objects.all().delete()
            items = [{"product_id": self.gallon.id, "quantity": 1, "unit_price": 4200 + n,
                      "extra_fields": self.colour_of(str(n))} for n in range(lines)]
            self.assertEqual(self.sale(items).status_code, 201)
            invoice = SalesInvoice.objects.get(business=self.business)
            with CaptureQueriesContext(connection) as queries:
                self.client.get(f"/sales-invoices/{invoice.id}/")
            return len(queries)
        self.assertEqual(count(2), count(6))


class ColourCodeOnPurchasesTests(LineFixture):

    def test_purchase_answers_its_own_fields(self):
        response = self.purchase([
            {"product_id": self.gallon.id, "quantity": 5, "unit_cost": 3000,
             "extra_fields": {str(self.batch.id): 42}},
            {"product_id": self.gallon.id, "quantity": 5, "unit_cost": 3000,
             "extra_fields": {str(self.batch.id): 43}},
        ])
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(PurchaseInvoiceItem.objects.filter(product=self.gallon).count(), 2)

    def test_same_product_at_two_costs_is_now_two_lines(self):
        response = self.purchase([
            {"product_id": self.brush.id, "quantity": 1, "unit_cost": 80},
            {"product_id": self.brush.id, "quantity": 1, "unit_cost": 90},
        ])
        self.assertEqual(response.status_code, 201, response.content)


class ProductFieldLinkTests(LineFixture):

    def product_body(self, links=None):
        body = {"name": "Weathershield", "unit": self.unit.id, "desc": "",
                "variants": [{"attributes": {"Size": "Gallon"}}]}
        if links is not None:
            body["field_links"] = links
        return body

    def test_create_and_replace_the_set(self):
        response = self.client.post("/products/create-with-variants/", self.product_body([
            {"field": self.colour.id, "applies_to": "both", "required": True}]), format="json")
        self.assertEqual(response.status_code, 201, response.content)
        product = Product.objects.get(name="Weathershield")
        self.assertEqual(list(product.field_links.values_list("field_id", "applies_to", "required")),
                         [(self.colour.id, "both", True)])

        body = self.product_body([{"field": self.batch.id, "applies_to": "purchase"}])
        response = self.client.post(f"/products/{product.id}/update-with-variants/", body, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(list(product.field_links.values_list("field_id", flat=True)), [self.batch.id])
        detail = self.client.get(f"/products/{product.id}/").json()
        self.assertEqual([link["label"] for link in detail["field_links"]], ["Batch no."])

        # Left out: the links stay as they are.
        response = self.client.post(f"/products/{product.id}/update-with-variants/",
                                    self.product_body(), format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(product.field_links.count(), 1)

    def test_another_business_field_is_a_400(self):
        response = self.client.post("/products/create-with-variants/", self.product_body([
            {"field": self.foreign_field.id}]), format="json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(Product.objects.filter(name="Weathershield").exists())

    def test_the_lists_carry_the_asked_fields(self):
        fields = self.client.get("/product-fields/").json()
        self.assertEqual([f["label"] for f in fields], ["Batch no.", "Colour code"])
        variants = self.client.get("/product-variants/").json()
        rows = variants["results"] if isinstance(variants, dict) else variants
        gallon = next(row for row in rows if row["id"] == self.gallon.id)
        self.assertEqual(
            [(f["label"], f["applies_to"], f["required"]) for f in gallon["base"]["extra_fields"]],
            [("Batch no.", "purchase", False), ("Colour code", "sale", True)])

    def test_a_field_in_use_is_protected_and_still_shows_when_retired(self):
        self.sale([{"product_id": self.gallon.id, "quantity": 1, "unit_price": 4200,
                    "extra_fields": self.colour_of("5674")}])
        with self.assertRaises(ProtectedError):
            self.colour.delete()
        self.colour.is_active = False
        self.colour.save()
        invoice = SalesInvoice.objects.get(business=self.business)
        line = self.client.get(f"/sales-invoices/{invoice.id}/").json()["invoice_items"][0]
        self.assertEqual(line["extra_fields_display"], [{"label": "Colour code", "value": "5674"}])


# ── part 02: line discounts ─────────────────────────────────────────────────

class LineDiscountRulesTests(TestCase):

    def test_clean(self):
        # Never empty: none, blank or zero is 0%.
        for raw in (None, "", {}, {"type": "percentage", "value": 0}, {"type": "amount", "value": 0}):
            with self.subTest(raw=raw):
                self.assertEqual(clean_line_discount(raw, 1000), {"type": "percentage", "value": 0})
        self.assertEqual(clean_line_discount({"type": "percentage", "value": "10.0"}, 1000),
                         {"type": "percentage", "value": 10})
        self.assertEqual(clean_line_discount({"type": "amount", "value": 250.555}, 1000),
                         {"type": "amount", "value": 250.56})
        bad = [({"type": "pct", "value": 1}, "percentage or an amount"),
               ({"type": "amount", "value": -1}, "negative"),
               ({"type": "amount", "value": "x"}, "number"),
               ({"type": "percentage", "value": 101}, "100%"),
               ({"type": "amount", "value": 1001}, "more than the line")]
        for raw, message in bad:
            with self.subTest(raw=raw):
                with self.assertRaisesMessage(serializers.ValidationError, message):
                    clean_line_discount(raw, 1000)

    def test_sums(self):
        self.assertEqual(line_total(100, 5, 5, {"type": "percentage", "value": 10}), 450)
        self.assertEqual(line_total(100, 5, 5, {"type": "amount", "value": 50}), 450)
        # Half returned: half the PKR discount.
        self.assertEqual(discount_off({"type": "amount", "value": 50}, 300, 6, 3), 25)
        self.assertEqual(line_total(100, 6, 3, {"type": "amount", "value": 50}), 275)


class LineDiscountOnSalesTests(LineFixture):

    def test_line_and_invoice_discount_tax_and_charges(self):
        response = self.sale([
            # 2 x 1000 at 10% = 1800; 1 x 500 less PKR 100 = 400.
            {"product_id": self.brush.id, "quantity": 2, "unit_price": 1000,
             "discount": {"type": "percentage", "value": 10}},
            {"product_id": self.quarter.id, "quantity": 1, "unit_price": 500,
             "extra_fields": self.colour_of("1453"), "discount": {"type": "amount", "value": 100}},
        ], discount={"value": 5, "type": "percentage"}, tax={"value": 10, "type": "percentage"},
            charges=[{"label": "Delivery", "amount": 300}])
        self.assertEqual(response.status_code, 201, response.content)
        invoice = SalesInvoice.objects.get(business=self.business)
        # sub_total after line discounts; the invoice's 5% and 10% tax on it.
        self.assertEqual(invoice.sub_total, 2200)
        self.assertAlmostEqual(invoice.total, 2200 - 110 + 220 + 300)

    def test_old_bugs(self):
        # A zero invoice discount no longer hides the line's; a percentage
        # takes off the whole line; an amount does not crash; it is saved.
        response = self.sale([
            {"product_id": self.brush.id, "quantity": 4, "unit_price": 100,
             "discount": {"type": "percentage", "value": 25}},
            {"product_id": self.gallon.id, "quantity": 1, "unit_price": 4200,
             "extra_fields": self.colour_of("5674"), "discount": {"type": "amount", "value": 200}},
        ])
        self.assertEqual(response.status_code, 201, response.content)
        invoice = SalesInvoice.objects.get(business=self.business)
        self.assertEqual(invoice.total, 300 + 4000)
        self.assertEqual(invoice.invoice_items.get(product=self.brush).discount,
                         {"type": "percentage", "value": 25})

    def test_a_discount_over_the_line_is_a_400(self):
        response = self.sale([{"product_id": self.brush.id, "quantity": 1, "unit_price": 100,
                               "discount": {"type": "amount", "value": 150}}])
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("Line 1: The discount is more than the line", str(response.content))

    def test_same_price_other_discount_is_another_line(self):
        response = self.sale([
            {"product_id": self.brush.id, "quantity": 1, "unit_price": 100,
             "discount": {"type": "percentage", "value": 10}},
            {"product_id": self.brush.id, "quantity": 1, "unit_price": 100},
        ])
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(SalesInvoiceItem.objects.filter(product=self.brush).count(), 2)
        twice = {"product_id": self.brush.id, "quantity": 1, "unit_price": 100,
                 "discount": {"type": "percentage", "value": 10}}
        self.assertEqual(self.sale([twice, dict(twice)], number="SAL-2").status_code, 400)

    def test_a_returned_half_takes_half_the_pkr_discount(self):
        self.sale([{"product_id": self.brush.id, "quantity": 4, "unit_price": 100,
                    "discount": {"type": "amount", "value": 40}}])
        invoice = SalesInvoice.objects.get(business=self.business)
        line = invoice.invoice_items.get()
        line.returned_quantity = 2
        line.save(update_fields=["returned_quantity"])
        invoice.adjust_totals()
        invoice.refresh_from_db()
        self.assertEqual(invoice.total, 200 - 20)

    def test_the_pdf_reads_line_totals(self):
        self.sale([{"product_id": self.brush.id, "quantity": 2, "unit_price": 100,
                    "discount": {"type": "percentage", "value": 50}}])
        invoice = SalesInvoice.objects.get(business=self.business)
        from .serializers import GenerateInvoiceSerializer
        line = GenerateInvoiceSerializer(invoice).data["invoice_items"][0]
        self.assertEqual((line["line_total"], line["discount"]),
                         (100, {"type": "percentage", "value": 50}))


class LineDiscountOnPurchasesTests(LineFixture):

    def test_a_purchase_discount_lowers_the_stock_cost_and_cogs(self):
        brush_stock = InventoryItem.objects.get(product=self.brush)
        brush_stock.delete()  # so the purchase creates it at its own cost
        response = self.purchase([{"product_id": self.brush.id, "quantity": 10, "unit_cost": 100,
                                   "discount": {"type": "percentage", "value": 10}}], status="R")
        self.assertEqual(response.status_code, 201, response.content)
        invoice = PurchaseInvoice.objects.get(business=self.business)
        self.assertEqual(invoice.total, 900)
        stock = InventoryItem.objects.get(product=self.brush)
        self.assertEqual((stock.quantity_on_hand, stock.unit_cost), (10, 90))

        self.assertEqual(self.sale([{"product_id": self.brush.id, "quantity": 1,
                                     "unit_price": 150}]).status_code, 201)
        today = date(2026, 11, 9)
        self.assertEqual(profit_estimate(self.business.id, today, today)["cost_of_goods_sold"], 90)


class NoDiscountIsZeroTests(LineFixture):
    """A line's discount is never empty or NULL: none is 0%."""

    def test_an_undiscounted_line_is_stored_as_zero(self):
        response = self.sale([{"product_id": self.brush.id, "quantity": 1, "unit_price": 100}],
                             number="ZERO-1")
        self.assertEqual(response.status_code, 201, response.content)
        line = SalesInvoiceItem.objects.get(sales_invoice__invoice_number="ZERO-1")
        self.assertEqual(line.discount, {"type": "percentage", "value": 0})
        # A line made without saying, as the old code during a deploy would.
        bare = SalesInvoiceItem.objects.create(
            business=self.business, sales_invoice=line.sales_invoice, product=self.brush,
            quantity=1, unit_price=50)
        self.assertEqual(bare.discount, {"type": "percentage", "value": 0})

    def test_purchase_line_too(self):
        response = self.purchase([{"product_id": self.brush.id, "quantity": 1, "unit_cost": 80}])
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(PurchaseInvoiceItem.objects.get(product=self.brush).discount,
                         {"type": "percentage", "value": 0})


class DiscountMigrationTests(TestCase):

    def test_0035_turns_empty_into_zero(self):
        migration = importlib.import_module("sales.migrations.0035_discount_never_empty")
        self.assertEqual(migration.NO_DISCOUNT, {"type": "percentage", "value": 0})

    def test_old_discounts_become_canonical(self):
        # 0034, frozen: its "none" was {}; 0035 then makes it 0%.
        migration = importlib.import_module("sales.migrations.0034_line_discounts")
        canonical = migration.canonical_discount
        self.assertEqual(canonical({"value": 0, "type": "percentage"}), {})
        self.assertEqual(canonical(None), {})
        self.assertEqual(canonical({"value": "x", "type": "amount"}), {})
        self.assertEqual(canonical({"value": 10.0, "type": "percentage"}),
                         {"type": "percentage", "value": 10})
