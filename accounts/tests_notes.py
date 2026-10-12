"""
Credit and debit notes through the API (credit-debit-notes plan, section 9).
"""
import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from root.models import Business, BusinessConfig, City, Customer, Supplier
from sales.models import PurchaseInvoice, PurchaseReceipt

from .models import (
    MoneyAccount, PartyOpeningBalance, PartyPayment, Transaction,
)
from .settlement import settle_parties
from .tests_settlement import noon
from .utils import (
    customer_balance, daily_summary, day_book, payables, profit_estimate,
    purchase_value_net_of_notes, receivables, revenue_net_of_notes,
    supplier_balance,
)

User = get_user_model()


class NotesAPITests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email='notes@example.com', password='pw')
        cls.business = Business.objects.create(
            name='Urban Paints', owner=cls.user, phone='0300', is_active=True)
        BusinessConfig.objects.create(business=cls.business, accounting=True)
        cls.account = MoneyAccount.objects.get_default_account(cls.business.id)
        cls.jotun = Supplier.objects.create(business=cls.business, name='Jotun')
        city = City.objects.create(name='Karachi', postal_code='74000')
        cls.amir = Customer.objects.create(
            business=cls.business, name='Amir', city=city)

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    # -- helpers ----------------------------------------------------------

    def purchase(self, number, total, day, status='R'):
        return PurchaseInvoice.objects.create(
            business=self.business, supplier=self.jotun, created_by=self.user,
            invoice_number=number, total=total, sub_total=total, status=status,
            is_restocked=True, tax={'value': 0, 'type': 'percentage'},
            date_issued=noon(*day))

    def create_note(self, **payload):
        body = {'type': 'supplier_credit_note', 'amount': 50000,
                'date': '2026-04-15', 'supplier': self.jotun.id,
                'reference': 'DWP-CN-118'}
        body.update(payload)
        return self.client.post('/transactions/', body, format='json')

    # -- validation -------------------------------------------------------

    def test_a_note_needs_no_account_and_is_cleared(self):
        response = self.create_note()
        self.assertEqual(response.status_code, 201, response.json())
        txn = Transaction.objects.get(type='supplier_credit_note')
        self.assertIsNone(txn.account_id)
        self.assertIsNone(txn.payment_method)
        self.assertEqual(txn.status, 'C')
        self.assertEqual(txn.party_payment.supplier_id, self.jotun.id)
        self.assertEqual(txn.direction, 'none')

    def test_a_note_rejects_an_account_or_a_cheque(self):
        for field, value in (('account', None), ('payment_method', 'cheque'),
                             ('cheque_number', '42')):
            value = self.account.id if field == 'account' else value
            response = self.create_note(**{field: value})
            self.assertEqual(response.status_code, 400, field)
            self.assertIn(field, response.json())

    def test_a_note_must_name_a_party_of_its_own_kind(self):
        response = self.create_note(supplier=None)
        self.assertEqual(response.status_code, 400)
        self.assertIn('supplier', response.json())

        response = self.create_note(supplier=None, customer=self.amir.id)
        self.assertEqual(response.status_code, 400)
        self.assertIn('customer', response.json())

    def test_a_note_cannot_name_an_invoice(self):
        invoice = self.purchase('INV-1', 50000, (2026, 1, 1))
        response = self.create_note(purchase_invoice=invoice.id)
        self.assertEqual(response.status_code, 400)
        self.assertIn('purchase_invoice', response.json())

    def test_money_types_still_require_an_account(self):
        response = self.client.post('/transactions/', {
            'type': 'supplier_payment', 'amount': 1000, 'date': '2026-04-15',
            'supplier': self.jotun.id}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('account', response.json())

    def test_a_note_can_be_edited_and_keeps_its_party(self):
        txn_id = self.create_note().json().get('id') or \
            Transaction.objects.get(type='supplier_credit_note').id
        response = self.client.put(f'/transactions/{txn_id}/', {
            'type': 'supplier_credit_note', 'amount': 30000,
            'date': '2026-04-16'}, format='json')
        self.assertEqual(response.status_code, 200, response.json())
        txn = Transaction.objects.get(pk=txn_id)
        self.assertEqual(txn.amount, 30000)
        self.assertEqual(txn.party_payment.supplier_id, self.jotun.id)

    def test_a_receipt_backed_transaction_cannot_be_edited_directly(self):
        invoice = self.purchase('INV-1', 50000, (2026, 1, 1))
        receipt = PurchaseReceipt.objects.create(
            purchase_invoice=invoice, amount=20000)
        txn = receipt.transaction_record
        response = self.client.put(f'/transactions/{txn.id}/', {
            'type': 'purchase_payment', 'amount': 10, 'date': '2026-01-02',
            'account': self.account.id, 'payment_method': 'cash'}, format='json')
        self.assertEqual(response.status_code, 400)
        txn.refresh_from_db()
        self.assertEqual(txn.amount, 20000)

    def test_detail_lists_what_the_note_settles(self):
        self.purchase('INV-1', 30000, (2026, 1, 1))
        self.purchase('INV-2', 30000, (2026, 2, 1))
        self.create_note(amount=40000)
        txn = Transaction.objects.get(type='supplier_credit_note')
        settles = self.client.get(f'/transactions/{txn.id}/').json()['settles']
        self.assertEqual([(row['label'], row['applied']) for row in settles],
                         [('INV-1', 30000), ('INV-2', 10000)])

    # -- cash figures do not move -----------------------------------------

    def test_a_note_never_reaches_a_cash_figure(self):
        day = datetime.date(2026, 4, 15)
        before = (self.account.balance(), Transaction.objects.money_in(
            self.business.id, day, day), Transaction.objects.money_out(
            self.business.id, day, day))
        book_before = day_book(self.business.id, day)

        self.create_note(type='supplier_credit_note')
        self.create_note(type='supplier_debit_note')

        after = (self.account.balance(), Transaction.objects.money_in(
            self.business.id, day, day), Transaction.objects.money_out(
            self.business.id, day, day))
        self.assertEqual(before, after)
        self.assertEqual(book_before['total_money_in'],
                         day_book(self.business.id, day)['total_money_in'])
        self.assertEqual(book_before['total_money_out'],
                         day_book(self.business.id, day)['total_money_out'])
        for kpi in ('cash_received', 'cash_paid'):
            self.assertEqual(getattr(Transaction.objects, kpi)(
                self.business.id, day, day), 0)

    # -- the khaata -------------------------------------------------------

    def test_khaata_rows_and_balance(self):
        self.purchase('INV-1', 100000, (2026, 1, 1))
        self.purchase('DRAFT-1', 70000, (2026, 1, 2), status='D')
        self.create_note(amount=30000)
        self.create_note(type='supplier_debit_note', amount=5000,
                         reference='FRT-7', date='2026-05-01')

        self.assertEqual(supplier_balance(self.jotun, self.business.id), 75000)
        rows = self.client.get(
            f'/accounting-kpis/party-ledger/?supplier_id={self.jotun.id}'
        ).json()['party_ledger']['rows']
        self.assertEqual(
            [(r['description'], r['naam'], r['jama']) for r in rows],
            [('Purchase invoice', 100000, 0), ('Credit note', 0, 30000),
             ('Debit note', 5000, 0)])

    def test_a_supplier_in_credit_is_listed_under_receivables(self):
        self.purchase('INV-1', 10000, (2026, 1, 1))
        self.create_note(amount=25000)
        rec = receivables(self.business.id)
        row = next(r for r in rec['parties'] if r['party_kind'] == 'supplier')
        self.assertEqual((row['id'], row['balance']), (self.jotun.id, 15000))
        self.assertNotIn(self.jotun.id, [r['id'] for r in payables(
            self.business.id)['parties'] if r['party_kind'] == 'supplier'])
        self.assertEqual(sum(rec['aging'].values()), rec['total'])

    def test_a_customer_in_credit_is_listed_under_payables(self):
        self.client.post('/transactions/', {
            'type': 'customer_credit_note', 'amount': 2500,
            'date': '2026-04-15', 'customer': self.amir.id}, format='json')
        self.assertEqual(customer_balance(self.amir, self.business.id), -2500)
        row = next(r for r in payables(self.business.id)['parties']
                   if r['party_kind'] == 'customer')
        self.assertEqual((row['id'], row['balance']), (self.amir.id, 2500))

    def test_the_balance_formula_equals_the_engine(self):
        PartyOpeningBalance.objects.create(
            business=self.business, supplier=self.jotun, amount=40000,
            as_of_date=datetime.date(2025, 12, 31))
        inv = self.purchase('INV-1', 100000, (2026, 1, 1))
        cancelled = self.purchase('C-1', 9000, (2026, 1, 3), status='C')
        PurchaseReceipt.objects.create(purchase_invoice=inv, amount=20000)
        PurchaseReceipt.objects.create(purchase_invoice=cancelled, amount=9000)
        self.create_note(amount=15000)
        self.create_note(type='supplier_debit_note', amount=3000)
        refund = Transaction.objects.create(
            business=self.business, account=self.account,
            type='purchase_return_refund', amount=2000,
            date=datetime.date(2026, 3, 1))
        PartyPayment.objects.create(business=self.business,
                                    supplier=self.jotun, transaction=refund)

        engine = settle_parties(self.business.id, 'supplier')[self.jotun.id]
        self.assertEqual(round(engine.balance),
                         supplier_balance(self.jotun, self.business.id))
        # 40k + 100k - 20k - 9k(paid on a cancelled invoice) - 15k + 3k + 2k
        self.assertEqual(round(engine.balance), 101000)

    # -- the payment picker -----------------------------------------------

    def test_picker_rooms_follow_the_pool(self):
        PartyOpeningBalance.objects.create(
            business=self.business, supplier=self.jotun, amount=10000,
            as_of_date=datetime.date(2025, 12, 31))
        first = self.purchase('INV-1', 30000, (2026, 1, 1))
        self.purchase('INV-2', 30000, (2026, 2, 1))
        self.purchase('DRAFT-9', 30000, (2026, 3, 1), status='D')
        self.create_note(amount=25000)   # opening 10k, INV-1 15k

        items = self.client.get(
            f'/accounting-kpis/settleable-items/?supplier_id={self.jotun.id}'
        ).json()['items']
        self.assertEqual([(i['label'], i['room']) for i in items],
                         [('INV-1', 15000), ('INV-2', 30000)])

        # A named payment above the room is refused, with the real limit.
        response = self.client.post('/transactions/', {
            'type': 'purchase_payment', 'amount': 20000, 'date': '2026-04-20',
            'account': self.account.id, 'payment_method': 'cash',
            'purchase_invoice': first.id}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('15,000', response.json()['amount'][0])

    def test_the_on_account_row_names_where_it_goes(self):
        PartyOpeningBalance.objects.create(
            business=self.business, supplier=self.jotun, amount=10000,
            as_of_date=datetime.date(2025, 12, 31))
        items = self.client.get(
            f'/accounting-kpis/settleable-items/?supplier_id={self.jotun.id}'
        ).json()['items']
        self.assertEqual(
            items[0]['sub'],
            'On account · settles the opening balance, then pending invoices')

    # -- the invoice side -------------------------------------------------

    def test_invoice_settlement_endpoint_feeds_the_payments_dialog(self):
        invoice = self.purchase('PUR-007', 208669, (2026, 5, 19))
        PurchaseReceipt.objects.create(purchase_invoice=invoice, amount=100000)
        self.create_note(amount=72338)
        body = self.client.get(
            f'/purchase-invoices/{invoice.id}/settlement/').json()
        self.assertEqual(body['paid_on_invoice'], 100000)
        self.assertEqual(body['settled_from_khaata'], 72338)
        self.assertEqual(body['still_owed'], 36331)
        self.assertEqual(body['room'], 36331)
        self.assertEqual([(row['label'], row['applied'])
                          for row in body['settled_by']],
                         [('Credit note', 72338)])

    def test_payment_status_filter_and_list_agree_with_the_khaata(self):
        older = self.purchase('P-1', 10000, (2026, 1, 1))
        newer = self.purchase('P-2', 10000, (2026, 2, 1))
        self.create_note(amount=15000)

        rows = {row['id']: row for row in
                self.client.get('/purchase-invoices/').json()}
        self.assertEqual(rows[older.id]['payment_status'], 'P')
        self.assertEqual(rows[newer.id]['payment_status'], 'PP')
        self.assertEqual(rows[newer.id]['amount_paid'], 5000)

        for value, expected in (('P', [older.id]), ('PP', [newer.id]),
                                ('PEN', [])):
            ids = [row['id'] for row in self.client.get(
                f'/purchase-invoices/?payment_status={value}').json()]
            self.assertEqual(ids, expected, value)

    # -- reporting (plan 5.5) ---------------------------------------------

    def test_profit_counts_notes_with_the_agreed_signs(self):
        day = datetime.date(2026, 4, 15)
        before = profit_estimate(self.business.id, day, day)
        self.create_note(type='supplier_credit_note', amount=10000)
        self.create_note(type='supplier_debit_note', amount=1000)
        self.client.post('/transactions/', {
            'type': 'customer_credit_note', 'amount': 3000,
            'date': '2026-04-15', 'customer': self.amir.id}, format='json')
        after = profit_estimate(self.business.id, day, day)
        self.assertEqual(after['revenue'] - before['revenue'], -3000)
        self.assertEqual(after['supplier_credits'], 9000)
        self.assertEqual(after['profit'] - before['profit'], 6000)

    def test_target_figures_net_off_notes_unless_filtered_by_status(self):
        start, end = datetime.date(2026, 4, 1), datetime.date(2026, 4, 30)
        self.purchase('INV-1', 50000, (2026, 4, 2))
        self.create_note(amount=8000)
        self.assertEqual(purchase_value_net_of_notes(
            self.business.id, start, end), 42000)
        self.assertEqual(purchase_value_net_of_notes(
            self.business.id, start, end, supplier_id=self.jotun.id), 42000)
        self.assertEqual(purchase_value_net_of_notes(
            self.business.id, start, end, status='R'), 50000)
        self.assertEqual(revenue_net_of_notes(self.business.id, start, end), 0)

    def test_pending_invoice_count_follows_the_khaata(self):
        self.purchase('P-1', 10000, (2026, 1, 1))
        self.purchase('P-2', 10000, (2026, 2, 1))
        self.purchase('D-1', 10000, (2026, 2, 2), status='D')
        self.purchase('C-1', 10000, (2026, 2, 3), status='C')
        self.create_note(amount=10000)
        self.assertEqual(
            PurchaseInvoice.objects.total_pending_invoices(self.business.id), 1)

    def test_daily_summary_totals_follow_the_khaata(self):
        self.purchase('INV-1', 10000, (2026, 1, 1))
        self.create_note(amount=25000)
        summary = daily_summary(self.business.id, datetime.date(2026, 4, 15))
        self.assertEqual(summary['total_receivable'], 15000)
        self.assertEqual(summary['total_payable'], 0)
