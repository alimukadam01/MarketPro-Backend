"""
Global search (root/search). Most tests run over the registry, so a type
added by a future module is checked by them too. The coverage test needs one
seeded record per type, and fails by name for a type without one.
"""
from datetime import date, datetime

from django.contrib.auth import get_user_model
from django.db.models import Q
from django.test import RequestFactory, TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import MoneyAccount, Transaction
from backlog.models import BacklogEntry
from inventory.models import InventoryItem
from projects.models import Project, ProjectTask
from root.models import (
    WALK_IN_CUSTOMER_NAME, Business, BusinessConfig, City, Customer, Employee,
    EmployeeAccess, Expense, Location, Product, ProductVariant, Supplier, Unit,
)
from root.search import Hit, SearchType, matching, registered
from root.search.access import Access
from root.search.engine import MAX_HITS, search
from root.search.registry import SearchRegistrationError, _REGISTRY, register
from sales.models import (
    InvoiceImage, PurchaseInvoice, PurchaseInvoiceItem, PurchaseQuotation,
    PurchaseQuotationItem, ReturnedItem, SalesInvoice, SalesInvoiceItem,
)
from targets.models import ManualDataPoint, Target

User = get_user_model()
TOKEN = "zqxv"


def noon(day):
    return timezone.make_aware(datetime(day.year, day.month, day.day, 12, 0))


class SearchFixture(TestCase):
    """Two businesses; every searchable type seeded once in the first."""

    @classmethod
    def setUpTestData(cls):
        cls.city = City.objects.create(name="Sukkur", postal_code="65200")
        cls.owner = User.objects.create_user(email="search-owner@example.com", password="pw-for-tests")
        cls.business = cls._business("Searchable Paints", cls.owner)
        cls.other_owner = User.objects.create_user(email="search-other@example.com", password="pw-for-tests")
        cls.other = cls._business("Other Paints", cls.other_owner)
        cls.seeded = cls._seed(cls.business, cls.owner)

    @classmethod
    def _business(cls, name, owner):
        business = Business.objects.create(name=name, owner=owner, phone="03001234567", is_active=True)
        BusinessConfig.objects.create(business=business, accounting=True, targets=True, projects=True)
        return business

    @classmethod
    def _seed(cls, business, owner, token=TOKEN):
        """One record per type, each carrying `token`. Returns {type: id}."""
        today = timezone.localdate()
        unit = Unit.objects.create(name=f"Tins {business.id}", abv="Tin")
        customer = Customer.objects.create(business=business, name=f"{token} customer", city=cls.city)
        supplier = Supplier.objects.create(business=business, name=f"{token} supplier")
        product = Product.objects.create(business=business, name=f"{token} product", unit=unit)
        other_product = Product.objects.create(business=business, name="Primer", unit=unit)
        variant = ProductVariant.objects.create(base=other_product, name="white", sku=f"{token}-SKU")
        plain_variant = ProductVariant.objects.create(base=product, name="plain", sku=f"P-{business.id}")
        sale = SalesInvoice.objects.create(
            business=business, customer=customer, invoice_number=f"{token}-S",
            created_by=owner, date_issued=noon(today), date_due=today, total=100)
        sale_item = SalesInvoiceItem.objects.create(
            business=business, sales_invoice=sale, product=plain_variant, quantity=2,
            unit_price=50, track_code=f"{token}-TRACK")
        purchase = PurchaseInvoice.objects.create(
            business=business, supplier=supplier, invoice_number=f"{token}-P",
            created_by=owner, date_issued=noon(today), status="D")
        purchase_item = PurchaseInvoiceItem.objects.create(
            business=business, purchase_invoice=purchase, product=plain_variant, quantity=1,
            unit_cost=40, notes=f"{token} purchase line")
        quotation = PurchaseQuotation.objects.create(business=business, quotation_no=f"{token}-Q", created_by=owner)
        quotation_item = PurchaseQuotationItem.objects.create(
            business=business, purchase_quotation=quotation, supplier=Supplier.objects.create(
                business=business, name="Quote supplier"),
            product=plain_variant, quantity=3, unit_price=10, notes=f"{token} quote line")
        returned = ReturnedItem.objects.create(
            business=business, invoice_item=sale_item, reason=f"{token} leaking tin",
            quantity=1, return_type="to_inventory")
        image = InvoiceImage.objects.create(sales_invoice=sale, image="invoices/x.png", caption=f"{token} receipt photo")
        stock = InventoryItem.objects.create(
            business=business, inventory=business.inventory_glance, product=plain_variant,
            track_code=f"{token}-STOCK")
        location = Location.objects.create(business=business, name=f"{token} godown")
        staff = User.objects.create_user(email=f"{token}-{business.id}@example.com", password="pw-for-tests",
                                         first_name=f"{token}", last_name="Staff")
        employee = Employee.objects.create(user=staff, business=business, created_by=owner)
        account = MoneyAccount.objects.create(business=business, name=f"{token} bank", type="bank")
        txn = Transaction.objects.create(
            business=business, account=account, type="other_income", amount=500,
            date=today, reference=f"{token}-REF")
        expense = Expense.objects.create(business=business, name=f"{token} rent", category="kiraya", amount=900)
        project = Project.objects.create(business=business, name=f"{token} tower")
        task = ProjectTask.objects.create(project=project, name=f"{token} paint lobby")
        target = Target.objects.create(business=business, name=f"{token} target", data_point="sales_revenue",
                                       period_type="month", period_year=today.year, period_month=today.month)
        point = ManualDataPoint.objects.create(business=business, name=f"{token} footfall", value=12)
        entry = BacklogEntry.objects.create(business=business, type="sales_invoice", image="backlog/x.png",
                                            created_by=owner, notes=f"{token} chase payment")
        return {
            "sales_invoice": sale.id, "sales_invoice_item": sale_item.id,
            "purchase_invoice": purchase.id, "purchase_invoice_item": purchase_item.id,
            "purchase_quotation": quotation.id, "purchase_quotation_item": quotation_item.id,
            "returned_item": returned.id, "invoice_image": image.id, "inventory_item": stock.id,
            "product": product.id, "product_variant": variant.id, "location": location.id,
            "customer": customer.id, "supplier": supplier.id, "employee": employee.id,
            "transaction": txn.id, "money_account": account.id, "expense": expense.id,
            "project": project.id, "project_task": task.id, "target": target.id,
            "manual_data_point": point.id, "backlog_entry": entry.id,
        }

    def request_for(self, user):
        request = RequestFactory().get("/global-search/")
        request.user = user
        return request

    def run_search(self, query, user=None, business=None):
        return search(self.request_for(user or self.owner), business or self.business, query)

    def groups(self, result):
        return {group["type"]: group for group in result["groups"]}


class RegistryContractTests(SearchFixture):
    """T1 - every registered type, including ones added later."""

    def test_every_type_is_complete_and_builds_its_query(self):
        access = Access(self.request_for(self.owner), self.business)
        self.assertGreaterEqual(len(registered()), 23)
        for search_type in registered():
            with self.subTest(search_type.key):
                self.assertTrue(search_type.label)
                self.assertTrue(search_type.module or search_type.access or search_type.queryset)
                condition, annotations = matching.build(search_type, "zq")
                queryset = search_type.base_queryset(self.request_for(self.owner), self.business, access)
                str(queryset.annotate(**annotations).filter(condition).query)

    def test_registration_refuses_a_bad_field_and_a_duplicate_key(self):
        bad = SearchType(key="broken", label="Broken", model=Customer, module="customers",
                         text_fields=["base__name"], to_hit=lambda row: Hit(id=row.id, title=""))
        with self.assertRaises(SearchRegistrationError):
            register(bad)
        self.assertNotIn("broken", _REGISTRY)
        duplicate = SearchType(key="customer", label="Again", model=Customer, module="customers",
                               text_fields=["name"], to_hit=lambda row: Hit(id=row.id, title=""))
        with self.assertRaises(SearchRegistrationError):
            register(duplicate)


class CoverageTests(SearchFixture):
    """T2, T3."""

    def test_every_registered_type_finds_its_record(self):
        found = self.groups(self.run_search(TOKEN))
        for search_type in registered():
            with self.subTest(search_type.key):
                self.assertIn(search_type.key, self.seeded,
                              f'Add a seed for the new search type "{search_type.key}" in SearchFixture._seed.')
                self.assertIn(search_type.key, found, f"{search_type.key} found nothing")
                ids = [hit["id"] for hit in found[search_type.key]["hits"]]
                self.assertIn(self.seeded[search_type.key], ids)
                hit = found[search_type.key]["hits"][0]
                self.assertTrue(hit["title"])
                self.assertTrue(hit["link"]["path"].startswith("/"))

    def test_products_and_returned_items_are_found(self):
        found = self.groups(self.run_search(f"{TOKEN} product"))
        self.assertIn("product", found)
        self.assertIn("returned_item", self.groups(self.run_search("leaking tin")))

    def test_nothing_crosses_businesses(self):
        theirs = self._seed(self.other, self.other_owner)
        for group in self.run_search(TOKEN)["groups"]:
            ids = {hit["id"] for hit in group["hits"]}
            self.assertNotIn(theirs[group["type"]], ids, group["type"])


class ResultShapeTests(SearchFixture):
    """T4, T5."""

    def test_hits_are_capped_and_total_is_the_full_count(self):
        for n in range(7):
            Customer.objects.create(business=self.business, name=f"Crowd {n}", city=self.city)
        group = self.groups(self.run_search("crowd"))["customer"]
        self.assertEqual(len(group["hits"]), MAX_HITS)
        self.assertEqual(group["total"], 7)

    def test_transactions_carry_a_breakdown_by_type(self):
        group = self.groups(self.run_search(TOKEN))["transaction"]
        self.assertIn("breakdown", group)
        self.assertEqual(sum(row["count"] for row in group["breakdown"]), group["total"])

    def test_no_match_has_no_groups_and_a_short_query_runs_nothing(self):
        self.assertEqual(self.run_search("nothing-is-called-this")["groups"], [])
        with self.assertNumQueries(0):
            self.assertEqual(self.run_search("c")["groups"], [])


class MatchingTests(SearchFixture):
    """T6, T7, T8."""

    def test_status_codes_are_not_matched_as_text(self):
        self.assertNotIn("sales_invoice", self.groups(self.run_search("completed")))

    def test_a_number_matches_exactly(self):
        customer = Customer.objects.get(id=self.seeded["customer"])
        hit = SalesInvoice.objects.create(business=self.business, customer=customer, invoice_number="AAA",
                                          created_by=self.owner, date_issued=noon(date.today()),
                                          date_due=date.today(), total=612)
        miss = SalesInvoice.objects.create(business=self.business, customer=customer, invoice_number="BBB",
                                           created_by=self.owner, date_issued=noon(date.today()),
                                           date_due=date.today(), total=1612)
        ids = [h["id"] for h in self.groups(self.run_search("612"))["sales_invoice"]["hits"]]
        self.assertIn(hit.id, ids)
        self.assertNotIn(miss.id, ids)

    def test_phones_match_however_they_are_written(self):
        customer = Customer.objects.create(business=self.business, name="Phone Person",
                                           phone="0300-7654321", city=self.city)
        for query in ("03007654321", "0300 7654321", "+923007654321", "7654321"):
            with self.subTest(query):
                ids = [h["id"] for h in self.groups(self.run_search(query))["customer"]["hits"]]
                self.assertIn(customer.id, ids)

    def test_choice_labels_find_their_codes(self):
        account = MoneyAccount.objects.get(id=self.seeded["money_account"])
        receipt = Transaction.objects.create(business=self.business, account=account,
                                             type="customer_receipt", amount=1200, date=date.today())
        ids = [h["id"] for h in self.groups(self.run_search("customer payment"))["transaction"]["hits"]]
        self.assertIn(receipt.id, ids)
        self.assertIn("expense", self.groups(self.run_search("kiraya")))

    def test_each_word_may_match_a_different_field(self):
        unit = Product.objects.get(id=self.seeded["product"]).unit
        fan = Product.objects.create(business=self.business, name="Royal Fan", unit=unit)
        white = ProductVariant.objects.create(base=fan, name="No Name / White", sku="RF-1")
        black = ProductVariant.objects.create(base=fan, name="Black", sku="RF-2")
        # The product's name and the variant's name are separate fields.
        ids = [h["id"] for h in self.groups(self.run_search("Royal Fan White"))["product_variant"]["hits"]]
        self.assertIn(white.id, ids)
        self.assertNotIn(black.id, ids)
        # Every word has to match something.
        self.assertNotIn("product_variant", self.groups(self.run_search("Royal Fan Purple")))
        # A word that is only punctuation is ignored, not matched.
        self.assertIn("product_variant", self.groups(self.run_search("Royal Fan / White")))


class PermissionTests(SearchFixture):
    """T9."""

    def employee(self, permissions):
        user = User.objects.create_user(email="limited@example.com", password="pw-for-tests", role="employee")
        employee = Employee.objects.create(user=user, business=self.business, created_by=self.owner)
        EmployeeAccess.objects.create(employee=employee, permissions=permissions)
        return user, employee

    def test_an_employee_sees_only_their_modules(self):
        user, _ = self.employee({"customers": {"view": True}, "expenses": {"view": False}})
        found = self.groups(self.run_search(TOKEN, user=user))
        self.assertIn("customer", found)
        for hidden in ("expense", "sales_invoice", "transaction", "money_account", "employee", "target"):
            self.assertNotIn(hidden, found)

    def test_a_module_switched_off_for_the_business_is_hidden_from_admins(self):
        BusinessConfig.objects.filter(business=self.business).update(returned_items=False)
        self.business.refresh_from_db()
        self.assertNotIn("returned_item", self.groups(self.run_search(TOKEN)))

    def test_backlog_shows_an_employee_only_their_open_entries(self):
        user, employee = self.employee({})
        mine = BacklogEntry.objects.create(business=self.business, type="sales_invoice", image="b.png",
                                           created_by=self.owner, assigned_to=employee, notes="zqxv mine")
        BacklogEntry.objects.create(business=self.business, type="sales_invoice", image="b.png",
                                    created_by=self.owner, assigned_to=employee, notes="zqxv done", is_done=True)
        ids = [h["id"] for h in self.groups(self.run_search(TOKEN, user=user))["backlog_entry"]["hits"]]
        self.assertEqual(ids, [mine.id])


class CostTests(SearchFixture):
    """T10 - the ceiling grows with the number of registered types."""

    def test_a_broad_search_stays_under_the_query_ceiling(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as queries:
            self.run_search(TOKEN)
        self.assertLessEqual(len(queries), 4 * len(registered()))


class EndpointTests(SearchFixture):

    def test_the_endpoint_answers(self):
        client = APIClient()
        client.force_authenticate(self.owner)
        response = client.get("/global-search/", {"q": TOKEN})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["groups"])
        self.assertEqual(client.get("/global-search/", {"q": "c"}).json()["groups"], [])
