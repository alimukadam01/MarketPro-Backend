"""
Recording a payment from the party screen (accounts/payments.py).

Ticking invoices is optional. A ticked invoice is settled by a payment that
names it; whatever is left goes on account as one customer/supplier payment,
which the khaata settles opening balance first, then oldest-first. A pending
on-account cheque holds back the room it will settle when it clears.
"""
import datetime

from django.test import TestCase
from rest_framework.test import APIClient

from root.models import BusinessConfig, City, Customer, Supplier
from sales.models import PurchaseInvoice, SalesInvoice
from sales.utils import invoice_room

from .models import MoneyAccount, PartyOpeningBalance, Transaction
from .tests_settlement import KhaataFixtures, noon


class RecordPartyPaymentTests(KhaataFixtures, TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.make_business('record-payment@example.com')
        BusinessConfig.objects.create(business=cls.business, accounting=True)
        cls.account = MoneyAccount.objects.get_default_account(cls.business.id)
        cls.bank = MoneyAccount.objects.create(
            business=cls.business, name='Meezan', type='bank')
        cls.jotun = Supplier.objects.create(business=cls.business, name='Jotun')
        city = City.objects.create(name='Karachi', postal_code='74000')
        cls.amir = Customer.objects.create(
            business=cls.business, name='Amir', city=city)

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    # -- helpers ----------------------------------------------------------

    def record(self, amount, invoices=(), party=None, **extra):
        party = party or self.jotun
        kind = 'customer' if isinstance(party, Customer) else 'supplier'
        body = {
            kind: party.id,
            'amount': amount,
            'items': [{'kind': 'invoice', 'id': inv.id} for inv in invoices],
            'account': self.account.id,
            'date': '2026-06-01',
            'payment_method': 'cash',
        }
        body.update(extra)
        return self.client.post('/transactions/record-payment/', body,
                                format='json')

    def items(self, party=None):
        party = party or self.jotun
        kind = 'customer' if isinstance(party, Customer) else 'supplier'
        return self.client.get(
            f'/accounting-kpis/settleable-items/?{kind}_id={party.id}').json()

    def fresh(self, invoice):
        return type(invoice).objects.get(pk=invoice.pk)

    def cheque(self, amount, invoices=()):
        return self.record(amount, invoices, account=self.bank.id,
                           payment_method='cheque', cheque_number='771')

    # -- nothing ticked ---------------------------------------------------

    def test_nothing_ticked_goes_on_account_and_settles_oldest_first(self):
        PartyOpeningBalance.objects.create(
            business=self.business, supplier=self.jotun, amount=10000,
            as_of_date=datetime.date(2025, 12, 31))
        first = self.purchase('INV-1', 30000, (2026, 1, 1))
        second = self.purchase('INV-2', 30000, (2026, 2, 1))

        response = self.record(25000)
        self.assertEqual(response.status_code, 201, response.json())
        body = response.json()
        self.assertEqual([(row['type'], row['amount']) for row in body],
                         [('supplier_payment', 25000)])

        # Opening 10k first, then 15k of INV-1; INV-2 untouched.
        self.assertEqual(self.items()['opening_balance']['remaining'], 0)
        self.assertEqual(self.fresh(first).amount_paid, 15000)
        self.assertEqual(self.fresh(first).payment_status, 'PP')
        self.assertEqual(self.fresh(second).amount_paid, 0)
        self.khaata()

    def test_more_than_is_owed_stays_on_the_khaata_as_credit(self):
        invoice = self.purchase('INV-1', 30000, (2026, 1, 1))

        response = self.record(50000)
        self.assertEqual(response.status_code, 201, response.json())

        self.assertEqual(self.fresh(invoice).payment_status, 'P')
        khaata = self.khaata()
        self.assertEqual(khaata.standing_credit, 20000)
        self.assertEqual(khaata.balance, -20000)

    def test_a_party_with_nothing_owed_can_pay_an_advance(self):
        self.assertEqual(self.items()['items'], [])
        response = self.record(5000)
        self.assertEqual(response.status_code, 201, response.json())
        self.assertEqual(self.khaata().standing_credit, 5000)

    def test_the_opening_balance_is_not_an_item(self):
        PartyOpeningBalance.objects.create(
            business=self.business, supplier=self.jotun, amount=10000,
            as_of_date=datetime.date(2025, 12, 31))
        self.purchase('INV-1', 30000, (2026, 1, 1))

        body = self.items()
        self.assertEqual([item['kind'] for item in body['items']], ['invoice'])
        self.assertEqual(body['opening_balance']['room'], 10000)

        response = self.record(
            1000, items=[{'kind': 'opening_balance', 'id': self.jotun.id}])
        self.assertEqual(response.status_code, 400)
        self.assertIn('items', response.json())

    # -- ticked invoices --------------------------------------------------

    def test_a_ticked_invoice_gets_a_named_payment_and_the_rest_goes_on_account(self):
        PartyOpeningBalance.objects.create(
            business=self.business, supplier=self.jotun, amount=10000,
            as_of_date=datetime.date(2025, 12, 31))
        first = self.purchase('INV-1', 30000, (2026, 1, 1))
        second = self.purchase('INV-2', 30000, (2026, 2, 1))

        # Tick only the newer invoice: it is filled by name, and the 20k left
        # settles the opening balance and then the older invoice.
        response = self.record(50000, [second])
        self.assertEqual(response.status_code, 201, response.json())
        body = response.json()
        self.assertEqual([(row['type'], row['amount']) for row in body],
                         [('purchase_payment', 30000),
                          ('supplier_payment', 20000)])
        self.assertEqual(len({row['payment_group'] for row in body}), 1)

        self.assertEqual(self.fresh(second).receipts_paid, 30000)
        self.assertEqual(self.fresh(second).payment_status, 'P')
        self.assertEqual(self.fresh(first).amount_paid, 10000)
        self.assertEqual(self.items()['opening_balance']['remaining'], 0)
        self.khaata()

    def test_an_amount_that_fits_the_ticked_invoices_writes_no_on_account_row(self):
        invoice = self.purchase('INV-1', 30000, (2026, 1, 1))
        response = self.record(30000, [invoice])
        self.assertEqual(response.status_code, 201, response.json())
        self.assertEqual([row['type'] for row in response.json()],
                         ['purchase_payment'])

    def test_a_ticked_invoice_the_amount_cannot_reach_is_refused(self):
        first = self.purchase('INV-1', 30000, (2026, 1, 1))
        second = self.purchase('INV-2', 30000, (2026, 2, 1))
        response = self.record(20000, [first, second])
        self.assertEqual(response.status_code, 400)
        self.assertIn('items', response.json())
        self.assertFalse(Transaction.objects.exists())

    def test_the_customer_side_mints_customer_types(self):
        invoice = SalesInvoice.objects.create(
            business=self.business, customer=self.amir, created_by=self.user,
            invoice_number='SI-1', total=8000, sub_total=8000, status='C',
            tax={'value': 0, 'type': 'percentage'}, date_issued=noon(2026, 1, 1))
        older = SalesInvoice.objects.create(
            business=self.business, customer=self.amir, created_by=self.user,
            invoice_number='SI-0', total=2000, sub_total=2000, status='C',
            tax={'value': 0, 'type': 'percentage'}, date_issued=noon(2025, 12, 1))

        response = self.record(9000, [invoice], party=self.amir)
        self.assertEqual(response.status_code, 201, response.json())
        self.assertEqual([(row['type'], row['amount']) for row in response.json()],
                         [('sale_payment', 8000), ('customer_receipt', 1000)])
        self.assertEqual(self.fresh(older).amount_paid, 1000)

    # -- pending cheques hold room ----------------------------------------

    def test_a_pending_on_account_cheque_holds_no_invoice(self):
        # The khaata only covers what an invoice's own payments leave unpaid
        # (owner's rule, 27 Nov), so on-account money, cleared or not, never
        # holds an invoice against its own payment.
        first = self.purchase('INV-1', 30000, (2026, 1, 1))
        second = self.purchase('INV-2', 30000, (2026, 2, 1))

        response = self.cheque(40000)
        self.assertEqual(response.status_code, 201, response.json())
        cheque = Transaction.objects.get(type='supplier_payment')
        self.assertEqual(cheque.status, 'PEN')

        self.assertEqual(self.fresh(first).amount_paid, 0)
        self.assertEqual(invoice_room(self.fresh(first)), 30000)
        self.assertEqual(invoice_room(self.fresh(second)), 30000)
        self.assertEqual(
            [(i['label'], i['room'], i['reserved'])
             for i in self.items()['items']],
            [('INV-1', 30000, 0), ('INV-2', 30000, 0)])

        response = self.client.post('/transactions/', {
            'type': 'purchase_payment', 'amount': 25000, 'date': '2026-06-02',
            'account': self.account.id, 'payment_method': 'cash',
            'purchase_invoice': second.id}, format='json')
        self.assertEqual(response.status_code, 201, response.content)

        # Bounced: nothing changes for the invoices' own room.
        response = self.client.post(f'/transactions/{cheque.id}/mark-bounced/')
        self.assertIn(response.status_code, (200, 204), response.content)
        self.assertEqual(invoice_room(self.fresh(first)), 30000)
        self.assertEqual(invoice_room(self.fresh(second)), 5000)

    def test_a_cleared_cheque_settles_what_it_held(self):
        first = self.purchase('INV-1', 30000, (2026, 1, 1))
        self.cheque(30000)
        cheque = Transaction.objects.get(type='supplier_payment')
        self.assertEqual(invoice_room(self.fresh(first)), 30000)

        response = self.client.post(f'/transactions/{cheque.id}/mark-cleared/')
        self.assertIn(response.status_code, (200, 204), response.content)
        self.assertEqual(self.fresh(first).payment_status, 'P')
        # Settled by the khaata, it can still take its own payment.
        self.assertEqual(invoice_room(self.fresh(first)), 30000)

    def test_a_pending_named_cheque_is_not_held_twice(self):
        # A pending cheque named on INV-1 holds INV-1 itself; on-account money
        # then reaches past it instead of being counted against it again.
        first = self.purchase('INV-1', 30000, (2026, 1, 1))
        second = self.purchase('INV-2', 30000, (2026, 2, 1))
        self.pay_invoice(first, 30000, status='PEN')
        self.party_txn('supplier_payment', 10000, (2026, 3, 1))

        self.assertEqual(invoice_room(self.fresh(first)), 0)
        self.assertEqual(invoice_room(self.fresh(second)), 30000)

    def test_an_invoice_the_khaata_settled_can_still_be_paid(self):
        invoice = self.purchase('INV-1', 30000, (2026, 1, 1))
        self.party_txn('supplier_payment', 30000, (2026, 3, 1))
        self.assertEqual(self.fresh(invoice).payment_status, 'P')
        self.assertEqual(invoice_room(self.fresh(invoice)), 30000)


class DailySummaryUdhaarTests(KhaataFixtures, TestCase):
    """
    Cash sales are payments made on their invoice's issue date. Whatever is
    not paid that day is credit extended, and any later payment on it is
    udhaar recovered - whichever way it was recorded, whatever the range.
    """

    TODAY = datetime.date(2026, 6, 1)

    @classmethod
    def setUpTestData(cls):
        cls.make_business('summary-udhaar@example.com')
        BusinessConfig.objects.create(business=cls.business, accounting=True)
        cls.account = MoneyAccount.objects.get_default_account(cls.business.id)
        city = City.objects.create(name='Karachi', postal_code='74000')
        cls.ali = Customer.objects.create(
            business=cls.business, name='Ali', city=city)

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def sale(self, number, total, day):
        return SalesInvoice.objects.create(
            business=self.business, customer=self.ali, created_by=self.user,
            invoice_number=number, total=total, sub_total=total, status='C',
            tax={'value': 0, 'type': 'percentage'}, date_issued=noon(*day))

    def pay(self, amount, invoices=(), day=None):
        response = self.client.post('/transactions/record-payment/', {
            'customer': self.ali.id, 'amount': amount,
            'items': [{'kind': 'invoice', 'id': inv.id} for inv in invoices],
            'account': self.account.id,
            'date': (day or self.TODAY).isoformat(),
            'payment_method': 'cash'}, format='json')
        self.assertEqual(response.status_code, 201, response.json())

    def summary(self, day=None, date_to=None):
        from .utils import daily_summary
        return daily_summary(self.business.id, day or self.TODAY, date_to)

    def assertSplit(self, summary, cash_sales, recovered, credit):
        self.assertEqual(summary['money_in']['cash_sales'], cash_sales)
        self.assertEqual(summary['money_in']['udhaar_recovered'], recovered)
        self.assertEqual(summary['credit_extended'], credit)

    def test_paying_an_old_invoice_by_name_is_udhaar_recovered(self):
        old = self.sale('SI-10', 50000, (2026, 5, 1))
        self.sale('SI-20', 30000, (2026, 6, 1))
        self.pay(50000, [old])
        self.assertSplit(self.summary(), 0, 50000, 30000)

    def test_paying_an_old_invoice_on_account_reads_the_same(self):
        self.sale('SI-10', 50000, (2026, 5, 1))
        self.sale('SI-20', 30000, (2026, 6, 1))
        self.pay(50000)
        self.assertSplit(self.summary(), 0, 50000, 30000)

    def test_paying_todays_invoice_by_name_is_a_cash_sale(self):
        today = self.sale('SI-20', 30000, (2026, 6, 1))
        self.pay(30000, [today])
        self.assertSplit(self.summary(), 30000, 0, 0)

    def test_a_later_payment_is_udhaar_recovered_even_inside_the_range(self):
        # Issued 1 May, paid 1 June, summarised May-June: the invoice was
        # credit on 1 May and the payment brought that udhaar back.
        old = self.sale('SI-10', 50000, (2026, 5, 1))
        self.pay(50000, [old])
        summary = self.summary(datetime.date(2026, 5, 1), self.TODAY)
        self.assertSplit(summary, 0, 50000, 50000)

    def test_a_payment_the_day_after_issue_is_udhaar_recovered(self):
        invoice = self.sale('SI-11', 20000, (2026, 5, 31))
        self.pay(20000, [invoice])
        self.assertSplit(self.summary(datetime.date(2026, 5, 31), self.TODAY),
                         0, 20000, 20000)

    def test_a_same_day_payment_inside_a_longer_range_is_a_cash_sale(self):
        invoice = self.sale('SI-12', 40000, (2026, 5, 10))
        self.pay(15000, [invoice], day=datetime.date(2026, 5, 10))
        self.pay(25000, [invoice])
        summary = self.summary(datetime.date(2026, 5, 1), self.TODAY)
        self.assertSplit(summary, 15000, 25000, 25000)

    def test_money_in_is_unchanged_by_the_split(self):
        old = self.sale('SI-10', 50000, (2026, 5, 1))
        today = self.sale('SI-20', 30000, (2026, 6, 1))
        self.pay(80000, [old, today])
        summary = self.summary()
        self.assertSplit(summary, 30000, 50000, 0)
        self.assertEqual(summary['money_in']['total'], 80000)

    # -- on-account money reaching the same day's invoice -----------------

    def test_on_account_money_paying_todays_invoice_is_a_cash_sale(self):
        self.sale('SI-20', 30000, (2026, 6, 1))
        self.pay(30000)
        self.assertSplit(self.summary(), 30000, 0, 0)

    def test_on_account_money_splits_between_old_udhaar_and_todays_sale(self):
        # The khaata clears the old invoice first; only what reaches today's
        # invoice is a cash sale.
        self.sale('SI-10', 20000, (2026, 5, 1))
        self.sale('SI-20', 30000, (2026, 6, 1))
        self.pay(35000)
        summary = self.summary()
        self.assertSplit(summary, 15000, 20000, 15000)
        self.assertEqual(summary['money_in']['total'], 35000)

    def test_on_account_money_the_next_day_is_udhaar_recovered(self):
        self.sale('SI-11', 20000, (2026, 5, 31))
        self.pay(20000)
        self.assertSplit(self.summary(datetime.date(2026, 5, 31), self.TODAY),
                         0, 20000, 20000)

    def test_a_credit_note_is_not_a_cash_sale(self):
        self.sale('SI-20', 30000, (2026, 6, 1))
        self.party_txn('customer_credit_note', 30000, (2026, 6, 1),
                       party=self.ali)
        self.assertSplit(self.summary(), 0, 0, 30000)
