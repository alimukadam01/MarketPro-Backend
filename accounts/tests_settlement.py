"""
The khaata settlement engine (accounts/settlement.py).

The fixture is the worked example in docs/credit-debit-notes-plan.md section 4:
Jotun's khaata with an opening balance and three invoices, and Urban's
target-rebate credit notes against it. Every test also checks the invariant
that remaining debts less leftover credits equals the balance.
"""
import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from root.models import Business, City, Customer, Supplier
from sales.models import PurchaseInvoice, PurchaseReceipt, SalesInvoice

from .models import (
    MoneyAccount, PartyOpeningBalance, PartyPayment, Transaction,
)
from .settlement import attach_settlement, settle_party

User = get_user_model()


def noon(y, m, d):
    # date_issued is a tz-aware datetime; noon keeps the local date stable.
    return timezone.make_aware(datetime.datetime(y, m, d, 12))


class KhaataFixtures:

    @classmethod
    def make_business(cls, email):
        cls.user = User.objects.create_user(email=email, password='pw')
        cls.business = Business.objects.create(
            name='Urban Paints', owner=cls.user, phone='0300', is_active=True)

    def purchase(self, number, total, day, status='R', supplier=None):
        return PurchaseInvoice.objects.create(
            business=self.business, supplier=supplier or self.jotun,
            created_by=self.user, invoice_number=number, total=total,
            sub_total=total, status=status, is_restocked=True,
            tax={'value': 0, 'type': 'percentage'}, date_issued=noon(*day))

    def pay_invoice(self, invoice, amount, status='C'):
        receipt = PurchaseReceipt.objects.create(
            purchase_invoice=invoice, amount=amount)
        if status != 'C':
            Transaction.objects.filter(purchase_receipt=receipt).update(
                status=status, payment_method='cheque', cheque_number='1')
        return receipt

    def party_txn(self, txn_type, amount, day, party=None, status='C',
                  reference=None):
        party = party or self.jotun
        is_note = txn_type in Transaction.NOTE_TYPES
        account = None if is_note else self.account
        txn = Transaction.objects.create(
            business=self.business, account=account, type=txn_type,
            amount=amount, date=datetime.date(*day), status=status,
            payment_method=None if is_note else 'cash', reference=reference)
        kind = 'customer' if isinstance(party, Customer) else 'supplier'
        PartyPayment.objects.create(
            business=self.business, transaction=txn, **{kind: party})
        return txn

    def khaata(self, party=None, kind='supplier'):
        party = party or self.jotun
        settlement = settle_party(self.business.id, kind, party.id)
        self.assertInvariant(settlement)
        return settlement

    def assertInvariant(self, settlement):
        self.assertAlmostEqual(
            settlement.outstanding - settlement.standing_credit,
            settlement.balance, places=2)

    def status_of(self, invoice):
        invoice = type(invoice).objects.get(pk=invoice.pk)
        return invoice.payment_status


class JotunWorkedExampleTests(KhaataFixtures, TestCase):
    """Plan section 4: each event in the worked example."""

    @classmethod
    def setUpTestData(cls):
        cls.make_business('urban@example.com')
        cls.account = MoneyAccount.objects.get_default_account(cls.business.id)
        cls.jotun = Supplier.objects.create(business=cls.business, name='Jotun')
        PartyOpeningBalance.objects.create(
            business=cls.business, supplier=cls.jotun, amount=200000,
            as_of_date=datetime.date(2026, 1, 1))

    def setUp(self):
        self.inv101 = self.purchase('INV-101', 300000, (2026, 2, 5))
        self.pay_invoice(self.inv101, 100000)
        self.inv102 = self.purchase('INV-102', 150000, (2026, 3, 20))
        self.inv103 = self.purchase('INV-103', 400000, (2026, 5, 2))

    def test_before_any_note_nothing_is_settled(self):
        khaata = self.khaata()
        self.assertEqual(khaata.balance, 950000)
        self.assertEqual(khaata.opening_remaining(), 200000)
        self.assertEqual(self.status_of(self.inv101), 'PP')   # its own receipt
        self.assertEqual(self.status_of(self.inv102), 'PEN')

    def test_credit_note_settles_opening_first_then_oldest(self):
        note = self.party_txn('supplier_credit_note', 450000, (2026, 4, 15),
                              reference='DWP-CN-118')
        khaata = self.khaata()

        self.assertEqual(khaata.balance, 500000)
        self.assertEqual(khaata.opening_remaining(), 0)
        self.assertEqual(self.status_of(self.inv101), 'P')
        self.assertEqual(self.status_of(self.inv102), 'PP')
        self.assertEqual(self.status_of(self.inv103), 'PEN')
        self.assertEqual(
            [(row['label'], row['applied']) for row in khaata.settles(note.id)],
            [('Opening balance', 200000), ('INV-101', 200000),
             ('INV-102', 50000)])

    def test_leftover_credit_stays_on_the_khaata(self):
        self.party_txn('supplier_credit_note', 1200000, (2026, 4, 15))
        khaata = self.khaata()
        self.assertEqual(khaata.balance, -250000)
        self.assertEqual(khaata.standing_credit, 250000)
        for invoice in (self.inv101, self.inv102, self.inv103):
            self.assertEqual(self.status_of(invoice), 'P')

    def test_a_new_invoice_is_settled_by_standing_credit_on_save(self):
        self.party_txn('supplier_credit_note', 1200000, (2026, 4, 15))
        inv104 = self.purchase('INV-104', 35000, (2026, 6, 1))
        self.assertEqual(self.status_of(inv104), 'P')
        self.assertEqual(self.khaata().standing_credit, 215000)

    def test_a_backdated_invoice_is_settled_before_newer_ones(self):
        self.party_txn('supplier_credit_note', 450000, (2026, 4, 15))
        inv090 = self.purchase('INV-090', 60000, (2026, 1, 10))
        # Opening 200k, INV-090 60k, INV-101 190k of its 200k.
        self.assertEqual(self.status_of(inv090), 'P')
        self.assertEqual(self.status_of(self.inv101), 'PP')
        self.assertEqual(self.status_of(self.inv102), 'PEN')

    def test_a_refund_closes_standing_credit(self):
        self.party_txn('supplier_credit_note', 1200000, (2026, 4, 15))
        self.party_txn('purchase_return_refund', 250000, (2026, 7, 1))
        khaata = self.khaata()
        self.assertEqual(khaata.balance, 0)
        self.assertEqual(khaata.standing_credit, 0)

    def test_deleting_the_note_reopens_what_it_settled(self):
        note = self.party_txn('supplier_credit_note', 450000, (2026, 4, 15))
        note.delete()
        khaata = self.khaata()
        self.assertEqual(khaata.opening_remaining(), 200000)
        self.assertEqual(self.status_of(self.inv101), 'PP')
        self.assertEqual(self.status_of(self.inv102), 'PEN')

    def test_editing_the_note_reflows(self):
        note = self.party_txn('supplier_credit_note', 450000, (2026, 4, 15))
        Transaction.objects.filter(pk=note.pk).update(amount=250000)
        self.assertEqual(self.status_of(self.inv101), 'PP')   # 50k reached it
        self.assertEqual(self.khaata().applied_to_invoice(self.inv101.id), 50000)


class PoolRuleTests(KhaataFixtures, TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.make_business('rules@example.com')
        cls.account = MoneyAccount.objects.get_default_account(cls.business.id)
        cls.jotun = Supplier.objects.create(business=cls.business, name='Jotun')

    def test_opening_balance_leads_even_when_an_invoice_is_older(self):
        PartyOpeningBalance.objects.create(
            business=self.business, supplier=self.jotun, amount=100000,
            as_of_date=datetime.date(2026, 3, 1))
        early = self.purchase('OLD-1', 80000, (2026, 1, 15))
        self.party_txn('supplier_payment', 120000, (2026, 4, 1))
        khaata = self.khaata()
        self.assertEqual(khaata.opening_remaining(), 0)
        self.assertEqual(khaata.applied_to_invoice(early.id), 20000)

    def test_a_negative_opening_balance_is_credit(self):
        PartyOpeningBalance.objects.create(
            business=self.business, supplier=self.jotun, amount=-50000,
            as_of_date=datetime.date(2026, 1, 1))
        invoice = self.purchase('P-1', 30000, (2026, 2, 1))
        self.assertEqual(self.status_of(invoice), 'P')
        self.assertEqual(self.khaata().balance, -20000)

    def test_drafts_and_cancelled_invoices_are_not_debts(self):
        draft = self.purchase('D-1', 50000, (2026, 1, 1), status='D')
        cancelled = self.purchase('C-1', 50000, (2026, 1, 2), status='C')
        live = self.purchase('R-1', 50000, (2026, 1, 3))
        self.party_txn('supplier_credit_note', 50000, (2026, 2, 1))
        khaata = self.khaata()
        self.assertEqual(khaata.balance, 0)
        self.assertEqual(khaata.applied_to_invoice(draft.id), 0)
        self.assertEqual(khaata.applied_to_invoice(cancelled.id), 0)
        self.assertEqual(self.status_of(live), 'P')

    def test_a_pending_on_account_cheque_settles_nothing(self):
        invoice = self.purchase('P-1', 40000, (2026, 1, 1))
        self.party_txn('supplier_payment', 40000, (2026, 2, 1), status='PEN')
        self.assertEqual(self.status_of(invoice), 'PEN')
        self.assertEqual(self.khaata().balance, 40000)

    def test_a_bounced_receipt_reopens_and_the_pool_flows_back(self):
        older = self.purchase('P-1', 40000, (2026, 1, 1))
        newer = self.purchase('P-2', 40000, (2026, 2, 1))
        self.pay_invoice(older, 40000)
        self.party_txn('supplier_credit_note', 40000, (2026, 3, 1))
        self.assertEqual(self.khaata().applied_to_invoice(newer.id), 40000)

        Transaction.objects.filter(purchase_receipt__purchase_invoice=older) \
            .update(status='B', payment_method='cheque', cheque_number='9')
        khaata = self.khaata()
        self.assertEqual(khaata.applied_to_invoice(older.id), 40000)
        self.assertEqual(khaata.applied_to_invoice(newer.id), 0)

    def test_a_debit_note_is_a_debt_a_later_credit_settles(self):
        self.purchase('P-1', 10000, (2026, 1, 1))
        freight = self.party_txn('supplier_debit_note', 20000, (2026, 2, 1))
        self.party_txn('supplier_payment', 25000, (2026, 3, 1))
        khaata = self.khaata()
        self.assertEqual(khaata.balance, 5000)
        # Invoice and transaction ids come from different tables and can
        # coincide, so an entry is identified by kind AND id.
        debt = next(d for d in khaata.debts
                    if d.kind == 'debit_note' and d.id == freight.id)
        self.assertEqual(debt.remaining, 5000)

    def test_an_overpaid_invoice_feeds_the_next_debt(self):
        first = self.purchase('P-1', 10000, (2026, 1, 1))
        second = self.purchase('P-2', 10000, (2026, 2, 1))
        self.pay_invoice(first, 15000)
        self.assertEqual(self.khaata().applied_to_invoice(second.id), 5000)

    def test_attach_settlement_runs_one_khaata_pass_per_party(self):
        invoices = [self.purchase(f'P-{n}', 1000, (2026, 1, n)) for n in range(1, 6)]
        self.party_txn('supplier_credit_note', 2500, (2026, 2, 1))
        fresh = list(PurchaseInvoice.objects.filter(pk__in=[i.pk for i in invoices])
                     .order_by('date_issued'))
        with self.assertNumQueries(4):   # openings, invoices + receipts x2, pool rows
            attach_settlement(fresh)
        self.assertEqual([i.payment_status for i in fresh],
                         ['P', 'P', 'PP', 'PEN', 'PEN'])


class CustomerMirrorTests(KhaataFixtures, TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.make_business('mirror@example.com')
        cls.account = MoneyAccount.objects.get_default_account(cls.business.id)
        city = City.objects.create(name='Karachi', postal_code='74000')
        cls.customer = Customer.objects.create(
            business=cls.business, name='Amir', city=city)

    def test_customer_credit_note_and_on_account_money_settle_sales_invoices(self):
        first = SalesInvoice.objects.create(
            business=self.business, customer=self.customer,
            created_by=self.user, invoice_number='SAL-1', total=79000,
            status='S', date_issued=noon(2026, 5, 16))
        second = SalesInvoice.objects.create(
            business=self.business, customer=self.customer,
            created_by=self.user, invoice_number='SAL-2', total=128000,
            status='S', date_issued=noon(2026, 5, 17))
        self.party_txn('customer_receipt', 100000, (2026, 6, 1),
                       party=self.customer)
        self.party_txn('customer_credit_note', 7000, (2026, 6, 2),
                       party=self.customer)
        khaata = self.khaata(self.customer, kind='customer')
        self.assertEqual(khaata.balance, 100000)
        self.assertEqual(self.status_of(first), 'P')
        self.assertEqual(khaata.applied_to_invoice(second.id), 28000)
        self.assertEqual(self.status_of(second), 'PP')
