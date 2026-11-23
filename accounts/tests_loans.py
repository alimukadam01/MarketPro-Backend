"""
People, Phase 4: loans with a person, set-offs, and the khaata by side.

Anyone can lend or borrow, whatever their roles (register #28). A loan taken
sits on Payable, a loan given on Receivable, in the same khaata as that
side's invoices (#3, #19). A repayment may name its loan (R18, #21). A
set-off settles both sides of one person by the same amount and moves no
money (#20, R25-R28). The khaata shows one side, or both with a running
balance each (#25).
"""
import datetime

from django.test import TestCase
from rest_framework.test import APIClient

from root.models import BusinessConfig, City, Customer, Person, Supplier
from root.people import side_balances
from sales.models import PurchaseInvoice

from .models import MoneyAccount, PartyPayment, Transaction
from .settlement import settle_party, settle_people
from .tests_settlement import KhaataFixtures, noon
from .utils import (
    customer_balance, daily_summary, payables, person_ledger, receivables,
    supplier_balance,
)


class LoanFixture(KhaataFixtures, TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.city = City.objects.create(name='Lahore', postal_code='54000')
        cls.make_business('loans@example.com')
        BusinessConfig.objects.create(business=cls.business, accounting=True)
        cls.account = MoneyAccount.objects.get_default_account(cls.business.id)
        cls.jotun = Supplier.objects.create(business=cls.business, name='Jotun')
        cls.ali = Customer.objects.create(business=cls.business, name='Ali Traders', city=cls.city)
        cls.rafiq = Person.objects.create(business=cls.business, name='Rafiq Ahmed')

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def loan(self, txn_type, amount, person, day=(2026, 1, 10), loan=None, status='C'):
        body = {'type': txn_type, 'amount': amount, 'date': datetime.date(*day).isoformat(),
                'account': self.account.id, 'payment_method': 'cash', 'person': person.id}
        if loan is not None:
            body['loan'] = loan.id
        if status == 'PEN':
            body.update(payment_method='cheque', cheque_number='9')
        response = self.client.post('/transactions/', body, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        return Transaction.objects.get(pk=response.json()['id'])

    def sides(self, person):
        return side_balances(self.business.id, [person.id]).get(person.id, {'receivable': 0, 'payable': 0})


class LoanKhaataTests(LoanFixture):

    def test_a_lender_with_no_role_is_on_payables(self):
        self.loan('loan_received', 500000, self.rafiq)
        self.assertEqual(self.sides(self.rafiq), {'receivable': 0, 'payable': 500000})
        rows = [row for row in payables(self.business.id)['parties'] if row['person_id'] == self.rafiq.id]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['id'], rows[0]['balance']), (None, 500000))
        khaata = settle_people(self.business.id, 'supplier', [self.rafiq.id])[self.rafiq.id]
        self.assertEqual(khaata.balance, 500000)

    def test_a_supplier_who_lends_has_one_payable_side(self):
        self.purchase('INV-1', 50000, (2026, 2, 1))
        self.loan('loan_received', 100000, self.jotun.person, day=(2026, 1, 15))
        self.assertEqual(self.khaata().balance, 150000)
        self.assertEqual(supplier_balance(self.jotun, self.business.id), 150000)
        # An unnamed payment settles the oldest first: the loan, then the invoice.
        self.party_txn('supplier_payment', 120000, (2026, 3, 1))
        khaata = self.khaata()
        self.assertEqual(khaata.balance, 30000)
        loan = Transaction.objects.get(type='loan_received')
        self.assertEqual(khaata.loan_status(loan.id)['status'], 'settled')
        self.assertEqual(self.status_of(PurchaseInvoice.objects.get(invoice_number='INV-1')), 'PP')

    def test_a_loan_given_sits_on_receivable_and_ages(self):
        self.loan('loan_given', 40000, self.ali.person, day=(2026, 1, 5))
        self.loan('loan_recovered', 15000, self.ali.person, day=(2026, 2, 5))
        self.assertEqual(customer_balance(self.ali, self.business.id), 25000)
        self.assertEqual(self.sides(self.ali.person), {'receivable': 25000, 'payable': 0})
        aging = settle_party(self.business.id, 'customer', self.ali.id).aging(today=datetime.date(2026, 6, 1))
        self.assertEqual(aging['days_over_60'], 25000)

    def test_a_named_repayment_goes_to_its_loan_first(self):
        self.purchase('INV-OLD', 80000, (2026, 1, 2))
        loan = self.loan('loan_received', 100000, self.jotun.person, day=(2026, 2, 1))
        self.loan('loan_repayment', 60000, self.jotun.person, day=(2026, 3, 1), loan=loan)
        khaata = self.khaata()
        self.assertEqual(khaata.balance, 120000)
        self.assertEqual(khaata.loan_status(loan.id),
                         {'amount': 100000, 'repaid': 60000, 'remaining': 40000, 'status': 'part'})
        # The older invoice was not touched.
        self.assertEqual(self.status_of(PurchaseInvoice.objects.get(invoice_number='INV-OLD')), 'PEN')
        self.assertEqual(supplier_balance(self.jotun, self.business.id), 120000)

    def test_repaying_more_than_the_loan_leaves_the_rest_on_the_khaata(self):
        self.purchase('INV-2', 30000, (2026, 4, 1))
        loan = self.loan('loan_received', 50000, self.jotun.person, day=(2026, 1, 1))
        self.loan('loan_repayment', 70000, self.jotun.person, day=(2026, 3, 1), loan=loan)
        khaata = self.khaata()
        self.assertEqual(khaata.loan_status(loan.id)['status'], 'settled')
        self.assertEqual(khaata.balance, 10000)   # 30,000 owed less the 20,000 extra
        self.assertEqual(self.status_of(PurchaseInvoice.objects.get(invoice_number='INV-2')), 'PP')

    def test_a_pending_repayment_cheque_changes_nothing_until_it_clears(self):
        loan = self.loan('loan_received', 50000, self.rafiq)
        self.loan('loan_repayment', 20000, self.rafiq, loan=loan, status='PEN')
        self.assertEqual(self.sides(self.rafiq)['payable'], 50000)

    def test_loans_stay_out_of_income_and_profit_but_have_their_own_line(self):
        self.loan('loan_received', 500000, self.rafiq, day=(2026, 5, 10))
        self.loan('loan_given', 20000, self.ali.person, day=(2026, 5, 10))
        summary = daily_summary(self.business.id, datetime.date(2026, 5, 10))
        self.assertEqual(summary['money_in']['loans'], 500000)
        self.assertEqual(summary['money_in']['other_income'], 0)
        self.assertEqual(summary['money_out']['loans'], 20000)
        for side in ('money_in', 'money_out'):
            lines = {k: v for k, v in summary[side].items() if k != 'total'}
            self.assertEqual(sum(lines.values()), summary[side]['total'], side)
        self.assertEqual(summary['profit']['profit'], 0)


class LoanValidationTests(LoanFixture):

    def post(self, **body):
        base = {'amount': 1000, 'date': '2026-01-01', 'account': self.account.id,
                'payment_method': 'cash'}
        return self.client.post('/transactions/', {**base, **body}, format='json')

    def test_a_loan_names_a_person_not_a_role(self):
        response = self.post(type='loan_given', customer=self.ali.id)
        self.assertEqual(response.status_code, 400)
        self.assertIn('person', response.json())

    def test_an_old_style_loan_without_a_person_is_still_accepted(self):
        response = self.post(type='loan_received')
        self.assertEqual(response.status_code, 201, response.content)
        self.assertFalse(PartyPayment.objects.filter(transaction_id=response.json()['id']).exists())

    def test_a_repayment_must_name_a_loan_of_its_kind_and_person(self):
        given = self.loan('loan_given', 5000, self.ali.person)
        received = self.loan('loan_received', 5000, self.rafiq)
        wrong_kind = self.post(type='loan_repayment', person=self.ali.person.id, loan=given.id)
        self.assertEqual(wrong_kind.status_code, 400)
        wrong_person = self.post(type='loan_repayment', person=self.ali.person.id, loan=received.id)
        self.assertEqual(wrong_person.status_code, 400)
        right = self.post(type='loan_repayment', person=self.rafiq.id, loan=received.id)
        self.assertEqual(right.status_code, 201, right.content)

    def test_a_set_off_cannot_be_entered_as_a_plain_transaction(self):
        response = self.post(type='set_off_payable', person=self.rafiq.id)
        self.assertEqual(response.status_code, 400)


class RecordPaymentLoanTests(LoanFixture):

    def test_a_ticked_loan_gets_a_repayment_that_names_it(self):
        self.purchase('INV-9', 40000, (2026, 1, 1))
        loan = self.loan('loan_received', 60000, self.jotun.person, day=(2026, 2, 1))
        items = self.client.get(f'/accounting-kpis/settleable-items/?supplier_id={self.jotun.id}').json()['items']
        self.assertIn(('loan', loan.id, 60000), [(i['kind'], i['id'], i['room']) for i in items])

        response = self.client.post('/transactions/record-payment/', {
            'supplier': self.jotun.id, 'amount': 70000,
            'items': [{'kind': 'loan', 'id': loan.id}],
            'account': self.account.id, 'date': '2026-06-01', 'payment_method': 'cash',
        }, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        types = sorted(row['type'] for row in response.json())
        self.assertEqual(types, ['loan_repayment', 'supplier_payment'])
        repayment = Transaction.objects.get(type='loan_repayment')
        self.assertEqual((repayment.amount, repayment.party_payment.loan_id), (60000, loan.id))
        khaata = self.khaata()
        self.assertEqual(khaata.loan_status(loan.id)['status'], 'settled')
        self.assertEqual(khaata.balance, 30000)


class SetOffTests(LoanFixture):

    def setUp(self):
        super().setUp()
        # Ali buys from the business and sells to it.
        self.ali_supplier = Supplier.objects.create(business=self.business, person=self.ali.person)
        self.sale = self.sell(30000)
        self.purchase('PI-0318', 50000, (2026, 3, 5), supplier=self.ali_supplier)

    def sell(self, total):
        from sales.models import SalesInvoice
        return SalesInvoice.objects.create(
            business=self.business, customer=self.ali, created_by=self.user,
            invoice_number='SI-1042', total=total, sub_total=total, status='C',
            tax={'value': 0, 'type': 'percentage'}, discount={'value': 0, 'type': 'amount'},
            date_issued=noon(2026, 3, 1))

    def set_off(self, amount, person=None):
        return self.client.post('/transactions/set-off/', {
            'person_id': (person or self.ali.person).id, 'amount': amount, 'date': '2026-04-01',
        }, format='json')

    def test_a_set_off_settles_both_sides_and_moves_no_money(self):
        before = MoneyAccount.objects.filter(pk=self.account.pk).with_balance().get().balance_value
        response = self.set_off(30000)
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertTrue(body['reference'].startswith('SO-20260401-'))
        self.assertEqual(self.sides(self.ali.person), {'receivable': 0, 'payable': 20000})
        halves = Transaction.objects.filter(type__in=Transaction.SET_OFF_TYPES)
        self.assertEqual(halves.count(), 2)
        self.assertTrue(all(t.account_id is None and t.direction == 'none' for t in halves))
        after = MoneyAccount.objects.filter(pk=self.account.pk).with_balance().get().balance_value
        self.assertEqual(before, after)
        self.assertEqual(self.status_of(self.sale), 'P')

    def test_a_set_off_is_at_most_the_smaller_side(self):
        response = self.set_off(30001)
        self.assertEqual(response.status_code, 400)
        self.assertIn('30,000', response.json()['detail'])

    def test_a_set_off_needs_both_sides(self):
        self.loan('loan_received', 5000, self.rafiq)
        self.assertEqual(self.set_off(1000, person=self.rafiq).status_code, 400)

    def test_editing_and_deleting_move_both_halves(self):
        group = self.set_off(10000).json()['group']
        edited = self.client.patch(f'/transactions/set-off/{group}/', {'amount': 30000}, format='json')
        self.assertEqual(edited.status_code, 200, edited.content)
        self.assertEqual(set(Transaction.objects.filter(payment_group=group).values_list('amount', flat=True)),
                         {30000})
        self.assertEqual(self.client.delete(f'/transactions/set-off/{group}/').status_code, 200)
        self.assertFalse(Transaction.objects.filter(payment_group=group).exists())
        self.assertEqual(self.sides(self.ali.person), {'receivable': 30000, 'payable': 50000})

    def test_the_khaata_shows_both_sides_with_a_balance_each(self):
        self.set_off(30000)
        both = person_ledger(self.business.id, self.ali.person, 'both')
        self.assertEqual(both['closing_balance'], {'receivable': 0, 'payable': 20000})
        set_off_rows = [row for row in both['rows'] if row['description'] == 'Set-off']
        self.assertEqual({row['side'] for row in set_off_rows}, {'receivable', 'payable'})
        last = both['rows'][-1]
        self.assertIn('receivable_balance', last)
        self.assertIn('payable_balance', last)
        one = person_ledger(self.business.id, self.ali.person, 'payable')
        self.assertEqual(one['closing_balance'], 20000)
        self.assertTrue(all('side' not in row for row in one['rows']))

    def test_the_khaata_endpoint_takes_a_person_and_side(self):
        response = self.client.get(
            f'/accounting-kpis/party-ledger/?person_id={self.ali.person.id}&side=receivable')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['party_ledger']['closing_balance'], 30000)

    def test_deleting_the_person_takes_the_set_off_and_loans_too(self):
        self.set_off(10000)
        self.loan('loan_given', 5000, self.ali.person)
        preview = self.client.get(f'/people/{self.ali.person.id}/delete-preview/').json()
        counted = {row['model']: row['count'] for row in preview['deleted']}
        self.assertEqual(counted['accounts.transaction'], 3)   # two halves and the loan
        response = self.client.delete(f'/people/{self.ali.person.id}/',
                                      {'confirm_name': 'Ali Traders'}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(Transaction.objects.filter(type__in=['loan_given', *Transaction.SET_OFF_TYPES]).exists())


class ReceivablesWithLoansTests(LoanFixture):

    def test_receivables_and_payables_include_people_with_loans_only(self):
        borrower = Person.objects.create(business=self.business, name='Kamran Iqbal')
        self.loan('loan_given', 20000, borrower)
        self.loan('loan_received', 100000, self.rafiq)
        owed = {row['person_id']: row['balance'] for row in receivables(self.business.id)['parties']}
        owing = {row['person_id']: row['balance'] for row in payables(self.business.id)['parties']}
        self.assertEqual(owed[borrower.id], 20000)
        self.assertEqual(owing[self.rafiq.id], 100000)
