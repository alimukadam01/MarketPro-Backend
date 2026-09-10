from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.admin_masking import MASK
from root.models import Business

from .models import MoneyAccount, PartyPayment, Transaction

User = get_user_model()

# A value unlike any pk, count or date on the page, so "not in the HTML" can be
# asserted with a plain substring check and no false negatives.
CANARY = 1234567


class MaskedAmountAdminTests(TestCase):
    """Currency amounts are masked in /admin/ but stay editable."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser(
            email="masking-test@example.com", password="pw-for-tests"
        )
        cls.business = Business.objects.create(
            name="Canary Traders", owner=cls.user, phone="03001234567"
        )
        # A cash account is created by accounts.signals.createDefaultMoneyAccount.
        cls.account = MoneyAccount.objects.get_default_account(cls.business.id)
        cls.transaction = Transaction.objects.create(
            business=cls.business,
            account=cls.account,
            type="customer_receipt",
            amount=CANARY,
            date="2026-01-15",
        )

    def setUp(self):
        self.client.force_login(self.user)

    def change_url(self):
        return reverse(
            "admin:accounts_transaction_change", args=[self.transaction.pk]
        )

    def post_data(self, **overrides):
        """Every editable field on the Transaction admin form."""
        data = {
            "business": self.business.pk,
            "account": self.account.pk,
            "transfer_account": "",
            "type": "customer_receipt",
            "amount": "",
            "date": "2026-01-15",
            "payment_method": "cash",
            "status": "C",
            "reference": "",
            "notes": "",
            "cheque_number": "",
            "cheque_due_date": "",
            "created_by": "",
            "sales_receipt": "",
            "purchase_receipt": "",
            "expense": "",
        }
        data.update(overrides)
        return data

    # -- the amount is absent from the response, not merely hidden ---------

    def test_amount_absent_from_change_form(self):
        response = self.client.get(self.change_url())
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, str(CANARY))
        self.assertContains(response, MASK)

    def test_change_form_input_carries_no_value_attribute(self):
        """input.html omits `value` entirely when the widget value is None."""
        response = self.client.get(self.change_url())
        html = response.content.decode()
        start = html.index('name="amount"')
        tag = html[html.rindex("<input", 0, start): html.index(">", start) + 1]
        self.assertNotIn("value=", tag)
        self.assertIn(MASK, tag)  # the placeholder

    def test_amount_absent_from_changelist(self):
        response = self.client.get(reverse("admin:accounts_transaction_changelist"))
        self.assertNotContains(response, str(CANARY))
        self.assertContains(response, MASK)

    def test_amount_absent_from_fk_dropdown_labels(self):
        """Transaction.__str__ embeds the amount; the <select> must not."""
        response = self.client.get(reverse("admin:accounts_partypayment_add"))
        self.assertNotContains(response, str(CANARY))
        self.assertContains(response, MASK)

    def test_amount_absent_from_fk_changelist_column(self):
        PartyPayment.objects.create(
            business=self.business, transaction=self.transaction
        )
        response = self.client.get(reverse("admin:accounts_partypayment_changelist"))
        self.assertNotContains(response, str(CANARY))

    # -- editing still works ----------------------------------------------

    def test_blank_amount_keeps_stored_value(self):
        """The load-bearing assertion: an untouched mask must not wipe the row."""
        response = self.client.post(
            self.change_url(), self.post_data(notes="edited elsewhere")
        )
        self.assertEqual(response.status_code, 302)

        self.transaction.refresh_from_db()
        self.assertEqual(self.transaction.amount, CANARY)
        self.assertEqual(self.transaction.notes, "edited elsewhere")

    def test_typed_amount_updates(self):
        response = self.client.post(self.change_url(), self.post_data(amount="999"))
        self.assertEqual(response.status_code, 302)

        self.transaction.refresh_from_db()
        self.assertEqual(self.transaction.amount, 999)

    def test_untouched_amount_is_not_logged_as_changed(self):
        """has_changed() keeps the mask out of the admin history."""
        self.client.post(self.change_url(), self.post_data(notes="only this"))

        entry = self.client.get(
            reverse("admin:accounts_transaction_history", args=[self.transaction.pk])
        )
        self.assertContains(entry, "Notes")
        self.assertNotContains(entry, "Amount")

    def test_blank_amount_on_add_falls_back_to_model_default(self):
        """amount has default=0, so a blank add is not an error -- it is 0."""
        response = self.client.post(
            reverse("admin:accounts_transaction_add"),
            self.post_data(type="other_income", date="2026-02-01"),
        )
        self.assertEqual(response.status_code, 302)

        created = Transaction.objects.get(type="other_income")
        self.assertEqual(created.amount, 0)

    # -- the mask cannot be defeated through the query string --------------

    def test_amount_lookup_is_rejected(self):
        """Otherwise ?amount__gt= binary-searches the hidden value."""
        response = self.client.get(
            reverse("admin:accounts_transaction_changelist"),
            {"amount__gt": "1000000"},
        )
        self.assertEqual(response.status_code, 400)

    def test_amount_column_is_not_sortable(self):
        response = self.client.get(reverse("admin:accounts_transaction_changelist"))
        self.assertNotContains(response, "column-amount")
        self.assertContains(response, "column-masked_amount")
