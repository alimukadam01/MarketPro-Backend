"""
The accounting fix plan, Phases 1, 3 and 4 (MarketPro Accounting Fix Plan,
27 Nov 2026).

1.1  A transfer between the shop's own accounts is not money in or out: it
     shows on both accounts and moves both balances, but stays out of the
     Book of Business totals. Cash counts get their own summary line.
1.2  A draft invoice is not a sale yet, in profit as on the khaata.
3.1  Debit and Credit the way the books put them: on Receivable a raise is a
     Debit, on Payable a raise is a Credit.
3.2  Every Book of Business row carries the account's balance after it; only
     cleared money moves it.
4.1  Same-day order on the khaata: invoices before the payments on them.
4.2  A payment recorded on an invoice but dated before it is kept for that
     invoice, so no other invoice uses it (Hirar Lal, Urban Paints).
"""
import datetime

from accounts.models import MoneyAccount, Transaction
from root.models import Customer, Person
from root.people import side_balances

from .tests_person_sides import PersonSidesFixture
from .tests_settlement import noon
from .utils import daily_summary, day_book, party_ledger, person_ledger, profit_estimate

DAY = datetime.date(2026, 3, 18)


class BookFixture(PersonSidesFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.bank = MoneyAccount.objects.create(
            business=cls.business, name='HBL', type='bank', opening_balance=0)

    def money(self, txn_type, amount, day=DAY, account=None, status='C', **extra):
        if status == 'PEN':
            extra.update(payment_method='cheque', cheque_number='77')
        return Transaction.objects.create(
            business=self.business, account=account or self.account, type=txn_type,
            amount=amount, date=day, status=status,
            payment_method=extra.pop('payment_method', 'cash'), **extra)

    def section(self, book, account):
        return next(s for s in book['accounts'] if s['account_id'] == account.id)


class TransfersInTheBookTests(BookFixture):

    def test_a_transfer_shows_on_both_accounts_but_not_in_the_totals(self):
        self.money('other_income', 100000)
        self.money('transfer', 50000, transfer_account=self.bank)
        book = day_book(self.business.id, DAY)

        self.assertEqual((book['total_money_in'], book['total_money_out']), (100000, 0))
        self.assertEqual(book['total_transfers'], 50000)
        cash, bank = self.section(book, self.account), self.section(book, self.bank)
        self.assertEqual((cash['money_in'], cash['money_out'], cash['closing_balance']),
                         (100000, 50000, 50000))
        self.assertEqual((bank['money_in'], bank['closing_balance']), (50000, 50000))
        self.assertEqual([row['money_in'] for row in bank['rows']], [50000])

        summary = daily_summary(self.business.id, DAY)
        self.assertEqual(summary['money_in']['total'], 100000)
        self.assertEqual(summary['money_out']['total'], 0)
        self.assertEqual(summary['transfers'], 50000)
        for side in ('money_in', 'money_out'):
            lines = {k: v for k, v in summary[side].items() if k != 'total'}
            self.assertEqual(sum(lines.values()), summary[side]['total'])

    def test_a_cash_count_has_its_own_line(self):
        self.money('other_income', 10000)
        self.money('cash_adjustment', -2000)
        self.money('cash_adjustment', 500)
        summary = daily_summary(self.business.id, DAY)
        self.assertEqual(summary['money_out']['cash_adjustments'], 2000)
        self.assertEqual(summary['money_out']['other'], 0)
        self.assertEqual(summary['money_in']['cash_adjustments'], 500)
        self.assertEqual(summary['money_in']['total'], 10500)


class BalanceOnEveryRowTests(BookFixture):

    def test_each_row_carries_the_balance_after_it(self):
        self.money('other_income', 10000, day=DAY - datetime.timedelta(days=1))
        self.money('other_income', 5000)
        self.money('supplier_payment', 3000, account=self.bank, status='PEN')
        self.money('expense', 1000)
        self.money('transfer', 4000, transfer_account=self.bank)
        book = day_book(self.business.id, DAY)

        cash = self.section(book, self.account)
        self.assertEqual(cash['opening_balance'], 10000)
        self.assertEqual([row['balance'] for row in cash['rows']], [15000, 14000, 10000])
        self.assertEqual(cash['rows'][-1]['balance'], cash['closing_balance'])

        # The pending cheque leaves the bank's balance where it was.
        bank = self.section(book, self.bank)
        self.assertEqual([(row['status'], row['balance']) for row in bank['rows']],
                         [('PEN', 0), ('C', 4000)])


class DraftsAreNotSalesTests(BookFixture):

    def test_a_draft_adds_nothing_to_revenue(self):
        from sales.models import SalesInvoice
        self.sell(10000, day=(2026, 3, 5))
        draft = self.sell(40000, day=(2026, 3, 6), number='SI-2')
        SalesInvoice.objects.filter(pk=draft.pk).update(status='D')
        profit = profit_estimate(self.business.id, datetime.date(2026, 3, 1),
                                 datetime.date(2026, 3, 31))
        self.assertEqual(profit['revenue'], 10000)


class DebitAndCreditBySideTests(BookFixture):

    def columns(self, ledger):
        return [(row.get('side'), row['description'], row['debit'], row['credit'])
                for row in ledger['rows']]

    def test_a_loan_received_is_a_credit_and_its_repayment_a_debit(self):
        received = self.loan('loan_received', 50000, self.rafiq, day=(2026, 1, 10))
        self.loan('loan_repayment', 20000, self.rafiq, day=(2026, 2, 10), loan=received)

        payable = person_ledger(self.business.id, self.rafiq, side='payable')
        self.assertEqual(payable['side'], 'payable')
        self.assertEqual([(row['debit'], row['credit'], row['balance']) for row in payable['rows']],
                         [(0, 50000, 50000), (20000, 0, 30000)])

        both = person_ledger(self.business.id, self.rafiq, side='both')
        self.assertEqual([(side, debit, credit) for side, _, debit, credit in self.columns(both)],
                         [('payable', 0, 50000), ('payable', 20000, 0)])

    def test_on_receivable_a_sale_is_a_debit_and_its_payment_a_credit(self):
        self.sell(10000)
        self.pay(customer=self.ali.id, amount=4000)
        ledger = person_ledger(self.business.id, self.ali.person, side='receivable')
        self.assertEqual([(row['debit'], row['credit']) for row in ledger['rows']],
                         [(10000, 0), (0, 4000)])

    def test_a_customers_advance_on_payable_is_a_credit(self):
        self.sell(10000)
        self.pay(customer=self.ali.id, amount=15000)
        both = person_ledger(self.business.id, self.ali.person, side='both')
        advance = [row for row in both['rows'] if row['side'] == 'payable']
        self.assertEqual([(row['debit'], row['credit']) for row in advance], [(0, 5000)])

    def test_a_suppliers_khaata_puts_the_purchase_in_credit(self):
        invoice = self.purchase('PI-1', 30000, (2026, 3, 1))
        self.pay_invoice(invoice, 10000)
        ledger = party_ledger(self.business.id, supplier=self.jotun)
        self.assertEqual(ledger['side'], 'payable')
        self.assertEqual([(row['description'], row['debit'], row['credit']) for row in ledger['rows']],
                         [('Purchase invoice', 0, 30000), ('Payment made', 10000, 0)])

    def test_every_party_type_lands_by_its_effect(self):
        from .utils import _debit_credit
        for txn_type, (party, effect, _) in Transaction.PARTY_EFFECTS.items():
            side = 'receivable' if party == 'customer' else 'payable'
            row = {'naam': 0, 'jama': 0}
            row['naam' if effect == 'adds' else 'jama'] = 100
            debit, credit = _debit_credit(row, side)
            raises = effect == 'adds'
            expected = (100, 0) if raises == (side == 'receivable') else (0, 100)
            self.assertEqual((debit, credit), expected, txn_type)


class KhaataOrderFixture(BookFixture):

    def sale(self, number, total, day):
        return self.sell(total, day=day, number=number)

    def pay_sale(self, invoice, amount, day):
        from sales.models import SalesReceipt
        receipt = SalesReceipt.objects.create(sales_invoice=invoice, amount=amount)
        Transaction.objects.filter(sales_receipt=receipt).update(date=datetime.date(*day))
        return receipt

    def rows(self, person):
        ledger = person_ledger(self.business.id, person, side='both')
        return ledger, [(row['date'], row['side'], row['description'], row['reference'],
                         row['debit'], row['credit'], row['receivable_balance'],
                         row['payable_balance']) for row in ledger['rows']]

    def assertClosesOnTheCards(self, person, ledger):
        found = side_balances(self.business.id, [person.id]).get(person.id)
        cards = (found['receivable'], found['payable']) if found else (0, 0)
        closing = ledger['closing_balance']
        self.assertEqual((closing['receivable'], closing['payable']), cards)


class SameDayOrderTests(KhaataOrderFixture):

    def test_an_invoice_comes_before_its_payment_whatever_was_typed_first(self):
        invoice = self.sale('SI-9', 10000, (2026, 3, 1))
        self.pay_sale(invoice, 10000, (2026, 3, 1))
        # The invoice typed in after its payment: walked by typing order, the
        # payment would come first and make an advance the invoice then uses.
        from sales.models import SalesInvoice
        SalesInvoice.objects.filter(pk=invoice.pk).update(
            created_at=noon(2099, 1, 1))
        ledger, rows = self.rows(self.ali.person)
        self.assertEqual([row[2] for row in rows], ['Sales invoice', 'Payment received'])
        self.assertFalse(any('advance' in row[2] for row in rows))
        self.assertClosesOnTheCards(self.ali.person, ledger)


class AdvanceKeptForItsInvoiceTests(KhaataOrderFixture):

    def test_hirar_lal(self):
        """The Urban Paints case: 303 typed before 304, both on 18 Mar."""
        inv_303 = self.sale('303', 70870, (2025, 3, 18))
        inv_304 = self.sale('304', 133000, (2025, 3, 18))
        self.pay_sale(inv_304, 30000, (2025, 1, 23))
        self.pay_sale(inv_303, 70870, (2025, 3, 18))
        self.pay_sale(inv_304, 50000, (2025, 3, 18))

        ledger, rows = self.rows(self.ali.person)
        self.assertEqual(rows, [
            ('2025-01-23', 'payable', 'Payment received · advance for its invoice', '304',
             0, 30000, 0, 30000),
            ('2025-03-18', 'receivable', 'Sales invoice', '303', 70870, 0, 70870, 30000),
            ('2025-03-18', 'receivable', 'Sales invoice', '304', 133000, 0, 203870, 30000),
            ('2025-03-18', 'receivable', 'Payment received · advance applied', '304',
             0, 30000, 173870, 30000),
            ('2025-03-18', 'payable', 'Payment received · advance used', '304',
             30000, 0, 173870, 0),
            ('2025-03-18', 'receivable', 'Payment received', '303', 0, 70870, 103000, 0),
            ('2025-03-18', 'receivable', 'Payment received', '304', 0, 50000, 53000, 0),
        ])
        self.assertEqual(ledger['closing_balance'], {'receivable': 53000, 'payable': 0})
        self.assertClosesOnTheCards(self.ali.person, ledger)

    def test_no_other_invoice_can_use_it(self):
        self.sale('SI-1', 10000, (2026, 1, 1))
        later = self.sale('SI-2', 5000, (2026, 2, 1))
        self.pay_sale(later, 5000, (2026, 1, 5))

        ledger, rows = self.rows(self.ali.person)
        jan_5 = [row for row in rows if row[0] == '2026-01-05']
        self.assertEqual([(row[1], row[6], row[7]) for row in jan_5],
                         [('payable', 10000, 5000)])
        self.assertEqual(ledger['closing_balance'], {'receivable': 10000, 'payable': 0})
        self.assertClosesOnTheCards(self.ali.person, ledger)

    def test_paid_ahead_beyond_the_invoice_leaves_the_rest_as_an_advance(self):
        invoice = self.sale('SI-1', 5000, (2026, 2, 1))
        self.pay_sale(invoice, 8000, (2026, 1, 5))
        ledger, rows = self.rows(self.ali.person)
        self.assertEqual(ledger['closing_balance'], {'receivable': 0, 'payable': 3000})
        self.assertClosesOnTheCards(self.ali.person, ledger)
        for row in rows:
            self.assertGreaterEqual(row[6], 0)
            self.assertGreaterEqual(row[7], 0)

    def test_a_supplier_paid_ahead_of_a_purchase(self):
        invoice = self.purchase('PI-7', 20000, (2026, 2, 1))
        receipt = self.pay_invoice(invoice, 20000)
        Transaction.objects.filter(purchase_receipt=receipt).update(date=datetime.date(2026, 1, 10))

        person = self.jotun.person
        ledger, rows = self.rows(person)
        self.assertEqual([(row[1], row[2], row[4], row[5]) for row in rows], [
            ('receivable', 'Payment made · advance for its invoice', 20000, 0),
            ('payable', 'Purchase invoice', 0, 20000),
            ('payable', 'Payment made · advance applied', 20000, 0),
            ('receivable', 'Payment made · advance used', 0, 20000),
        ])
        self.assertEqual(ledger['closing_balance'], {'receivable': 0, 'payable': 0})
        self.assertClosesOnTheCards(person, ledger)

    def test_a_payment_on_or_after_its_invoice_date_walks_as_before(self):
        invoice = self.sale('SI-1', 10000, (2026, 1, 1))
        self.pay_sale(invoice, 4000, (2026, 1, 1))
        _, rows = self.rows(self.ali.person)
        self.assertFalse(any('advance' in row[2] for row in rows))


class InvoicePaysItselfFirstTests(KhaataOrderFixture):
    """
    An invoice takes all its own payments first; the khaata's advance only
    covers what they leave unpaid (owner's rule, 27 Nov 2026). So an invoice
    the advance covers can still be paid on its own, the advance stays, and
    the khaata shows no advance moving. A payment can't be dated before its
    invoice: money paid ahead is a Customer or Supplier Payment.
    """

    def advance(self, amount, day='2026-03-01'):
        response = self.pay(customer=self.ali.id, amount=amount, date=day)
        self.assertEqual(response.status_code, 201, response.content)

    def sale_payment(self, invoice, amount, day):
        return self.client.post('/transactions/', {
            'type': 'sale_payment', 'amount': amount, 'date': day,
            'account': self.account.id, 'payment_method': 'cash',
            'sales_invoice': invoice.id}, format='json')

    def test_paying_an_invoice_the_advance_covers_keeps_the_advance(self):
        self.advance(10000)
        invoice = self.sale('INV-50', 10000, (2026, 3, 5))
        self.assertEqual(self.sides(self.ali.person), (0, 0))

        response = self.sale_payment(invoice, 10000, '2026-03-06')
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self.sides(self.ali.person), (0, 10000))

        ledger, rows = self.rows(self.ali.person)
        self.assertEqual([(r[1], r[2], r[4], r[5], r[6], r[7]) for r in rows], [
            ('payable', 'On-account payment · advance', 0, 10000, 0, 10000),
            ('receivable', 'Sales invoice', 10000, 0, 10000, 10000),
            ('receivable', 'Payment received', 0, 10000, 0, 10000),
        ])
        self.assertClosesOnTheCards(self.ali.person, ledger)

        # The invoice dialog's figures say the same.
        body = self.client.get(f'/sales-invoices/{invoice.id}/settlement/').json()
        self.assertEqual((body['paid_on_invoice'], body['settled_from_khaata'],
                          body['room']), (10000, 0, 0))

    def test_part_paid_the_advance_covers_the_rest(self):
        self.advance(10000)
        invoice = self.sale('INV-51', 10000, (2026, 3, 5))
        self.sale_payment(invoice, 6000, '2026-03-06')

        ledger, rows = self.rows(self.ali.person)
        self.assertEqual([(r[1], r[2], r[4], r[5]) for r in rows], [
            ('payable', 'On-account payment · advance', 0, 10000),
            ('receivable', 'Sales invoice', 10000, 0),
            ('receivable', 'Sales invoice · advance applied', 0, 4000),
            ('payable', 'Sales invoice · advance used', 4000, 0),
            ('receivable', 'Payment received', 0, 6000),
        ])
        self.assertEqual(ledger['closing_balance'], {'receivable': 0, 'payable': 6000})
        self.assertClosesOnTheCards(self.ali.person, ledger)

    def test_the_invoice_shows_its_full_amount(self):
        self.advance(20000)
        invoice = self.sale('INV-54', 9000, (2026, 3, 5))
        self.sale_payment(invoice, 4500, '2026-03-05')
        ledger, rows = self.rows(self.ali.person)
        invoice_rows = [(r[1], r[2], r[4], r[5]) for r in rows if r[3] == 'INV-54']
        self.assertEqual(invoice_rows, [
            ('receivable', 'Sales invoice', 9000, 0),
            ('receivable', 'Sales invoice · advance applied', 0, 4500),
            ('payable', 'Sales invoice · advance used', 4500, 0),
            ('receivable', 'Payment received', 0, 4500),
        ])
        self.assertEqual(ledger['closing_balance'], {'receivable': 0, 'payable': 15500})
        self.assertClosesOnTheCards(self.ali.person, ledger)

    def test_an_unpaid_invoice_still_uses_the_advance(self):
        self.advance(10000)
        self.sale('INV-52', 4000, (2026, 3, 5))
        ledger, rows = self.rows(self.ali.person)
        self.assertEqual([(r[1], r[2], r[4], r[5]) for r in rows][1:], [
            ('receivable', 'Sales invoice', 4000, 0),
            ('receivable', 'Sales invoice · advance applied', 0, 4000),
            ('payable', 'Sales invoice · advance used', 4000, 0),
        ])
        self.assertEqual(ledger['closing_balance'], {'receivable': 0, 'payable': 6000})

    def test_a_payment_before_its_invoice_is_refused(self):
        invoice = self.sale('INV-53', 10000, (2026, 3, 5))

        response = self.sale_payment(invoice, 1000, '2026-03-04')
        self.assertEqual(response.status_code, 400)
        self.assertIn('Customer Payment', response.json()['date'][0])

        response = self.client.post(f'/sales-invoices/{invoice.id}/payments/', {
            'amount': 1000, 'account': self.account.id, 'payment_method': 'cash',
            'date': '2026-03-04'}, format='json')
        self.assertEqual(response.status_code, 400)

        response = self.pay(customer=self.ali.id, amount=1000, date='2026-03-04',
                            items=[{'kind': 'invoice', 'id': invoice.id}])
        self.assertEqual(response.status_code, 400)

        # On the day itself, it is fine.
        self.assertEqual(self.sale_payment(invoice, 1000, '2026-03-05').status_code, 201)

    def test_a_supplier_payment_before_its_purchase_is_refused(self):
        invoice = self.purchase('PI-9', 20000, (2026, 2, 1))
        response = self.client.post('/transactions/', {
            'type': 'purchase_payment', 'amount': 1000, 'date': '2026-01-31',
            'account': self.account.id, 'payment_method': 'cash',
            'purchase_invoice': invoice.id}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('Supplier Payment', response.json()['date'][0])
