"""
Receivable and Payable belong to the person, not the roles.

- Opening balances are the person's, one per side, whatever roles they hold.
  A Payable opening for someone who is only a customer is an advance their
  next sale uses up; a Receivable opening for someone who is only a supplier
  is used by the next purchase. Otherwise each side counts on its own khaata.
- The khaata shows a payment that takes a side past zero in two parts, the
  rest on the other side as an advance. It stays one transaction.
- Record Payment pays back the other side's advance first: 'You pay'
  refunds a customer's advance, then pays supplier dues.
"""
import datetime

from accounts.models import PartyOpeningBalance, Transaction
from root.models import Supplier
from root.people import side_balances

from .tests_loans import LoanFixture
from .tests_settlement import noon
from .utils import party_ledger, person_ledger


class PersonSidesFixture(LoanFixture):

    def sell(self, total, day=(2026, 3, 1), number='SI-1'):
        from sales.models import SalesInvoice
        return SalesInvoice.objects.create(
            business=self.business, customer=self.ali, created_by=self.user,
            invoice_number=number, total=total, sub_total=total, status='C',
            tax={'value': 0, 'type': 'percentage'}, discount={'value': 0, 'type': 'amount'},
            date_issued=noon(*day))

    def opening(self, person, side, amount, day=(2025, 12, 31)):
        return PartyOpeningBalance.objects.create(
            business=self.business, person=person, side=side,
            kind=PartyOpeningBalance.opening_kind(person, side),
            amount=amount, as_of_date=datetime.date(*day))

    def pay(self, **body):
        body.setdefault('account', self.account.id)
        body.setdefault('date', '2026-04-01')
        body.setdefault('payment_method', 'cash')
        return self.client.post('/transactions/record-payment/', body, format='json')

    def sides(self, person):
        found = side_balances(self.business.id, [person.id]).get(person.id)
        return (found['receivable'], found['payable']) if found else (0, 0)


class OpeningOnThePersonTests(PersonSidesFixture):

    def test_a_customers_payable_opening_is_an_advance_the_next_sale_uses(self):
        row = self.opening(self.ali.person, 'payable', 5000)
        self.assertEqual(row.kind, 'customer')
        self.assertEqual(self.sides(self.ali.person), (0, 5000))
        self.sell(8000)
        self.assertEqual(self.sides(self.ali.person), (3000, 0))

    def test_with_both_roles_the_payable_opening_waits_for_its_own_side(self):
        Supplier.objects.create(business=self.business, person=self.ali.person)
        row = self.opening(self.ali.person, 'payable', 5000)
        self.assertEqual(row.kind, 'supplier')
        self.sell(8000)
        self.assertEqual(self.sides(self.ali.person), (8000, 5000))

    def test_someone_with_no_role_carries_both_sides(self):
        self.opening(self.rafiq, 'receivable', 300)
        self.opening(self.rafiq, 'payable', 700)
        self.assertEqual(self.sides(self.rafiq), (300, 700))

    def test_the_old_role_form_still_counts_where_it_did(self):
        row = PartyOpeningBalance.objects.create(
            business=self.business, customer=self.ali, amount=-2500,
            as_of_date=datetime.date(2025, 12, 31))
        self.assertEqual((row.person_id, row.side, row.kind, row.amount),
                         (self.ali.person.id, 'payable', 'customer', 2500))
        self.assertEqual(self.ali.opening_balance.amount, -2500)

    def test_the_person_page_shows_each_role_balance_for_deactivating(self):
        self.opening(self.ali.person, 'payable', 5000)
        body = self.client.get(f'/people/{self.ali.person.id}/').json()
        self.assertEqual(body['roles']['customer']['balance'], -5000)
        self.assertEqual(body['opening_balances']['payable']['amount'], 5000)
        self.assertIsNone(body['opening_balances']['receivable'])

    def test_one_opening_per_side_of_a_person(self):
        self.opening(self.rafiq, 'payable', 700)
        response = self.client.post('/party-opening-balances/', {
            'person': self.rafiq.id, 'side': 'payable', 'amount': 5, 'as_of_date': '2026-01-01',
        }, format='json')
        self.assertEqual(response.status_code, 400)


class KhaataBySideTests(PersonSidesFixture):

    def test_an_overpayment_shows_its_excess_on_payable_as_one_transaction(self):
        self.sell(10000)
        response = self.pay(customer=self.ali.id, amount=15000)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(len(response.json()), 1)

        ledger = person_ledger(self.business.id, self.ali.person, side='both')
        payment = [row for row in ledger['rows'] if row['description'].startswith('On-account')]
        self.assertEqual([(row['side'], row['naam'], row['jama']) for row in payment],
                         [('receivable', 0, 10000), ('payable', 5000, 0)])
        self.assertTrue(payment[1]['description'].endswith('· advance'))
        self.assertEqual(ledger['closing_balance'], {'receivable': 0, 'payable': 5000})
        for row in ledger['rows']:
            self.assertGreaterEqual(row['receivable_balance'], 0)
            self.assertGreaterEqual(row['payable_balance'], 0)

        payable = person_ledger(self.business.id, self.ali.person, side='payable')
        self.assertEqual(payable['closing_balance'], 5000)

    def test_the_next_sale_uses_the_advance_on_payable_first(self):
        self.sell(10000)
        self.pay(customer=self.ali.id, amount=15000)
        self.sell(8000, day=(2026, 5, 1), number='SI-2')
        ledger = person_ledger(self.business.id, self.ali.person, side='both')
        sale = [row for row in ledger['rows'] if row['reference'] == 'SI-2']
        # The sale at its full amount, then the advance applied against it.
        self.assertEqual([(row['side'], row['description'], row['naam'], row['jama'])
                          for row in sale],
                         [('receivable', 'Sales invoice', 8000, 0),
                          ('receivable', 'Sales invoice · advance applied', 0, 5000),
                          ('payable', 'Sales invoice · advance used', 0, 5000)])
        self.assertEqual(ledger['closing_balance'], {'receivable': 3000, 'payable': 0})
        self.assertEqual(self.sides(self.ali.person), (3000, 0))


class PayingBackAnAdvanceTests(PersonSidesFixture):

    def test_you_pay_a_customer_refunds_their_advance_without_a_supplier_role(self):
        self.opening(self.ali.person, 'payable', 5000)
        items = self.client.get(
            f'/accounting-kpis/settleable-items/?person_id={self.ali.person.id}&direction=out').json()
        self.assertEqual((items['has_role'], items['advance']['room']), (False, 5000))

        too_much = self.pay(person=self.ali.person.id, direction='out', amount=6000)
        self.assertEqual(too_much.status_code, 400)

        response = self.pay(person=self.ali.person.id, direction='out', amount=5000)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual([row['type'] for row in response.json()], ['sales_return_refund'])
        self.assertEqual(self.sides(self.ali.person), (0, 0))

    def test_a_mixed_payable_is_paid_as_a_refund_and_a_supplier_payment(self):
        supplier = Supplier.objects.create(business=self.business, person=self.ali.person)
        self.sell(10000)
        self.pay(customer=self.ali.id, amount=15000)  # 5,000 advance on Payable
        self.purchase('PI-1', 20000, (2026, 3, 5), supplier=supplier)
        self.assertEqual(self.sides(self.ali.person), (0, 25000))

        response = self.pay(supplier=supplier.id, amount=25000, date='2026-06-01')
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(sorted((row['type'], row['amount']) for row in response.json()),
                         [('sales_return_refund', 5000), ('supplier_payment', 20000)])
        self.assertEqual(self.sides(self.ali.person), (0, 0))
        self.assertEqual(
            Transaction.objects.filter(type__in=['sales_return_refund', 'supplier_payment'])
            .values('payment_group').distinct().count(), 1)


class FractionalAmountsTests(PersonSidesFixture):
    """
    Invoice totals carry fractions. The khaata must still close on the cards'
    figure, which rounds each role's balance once (2026-11-23 production
    check: rounding each row first left two khaatas 1 and 2 PKR short).
    """

    def test_the_khaata_closes_on_the_card_when_totals_carry_fractions(self):
        for day, number in ((1, 'SI-1'), (2, 'SI-2'), (3, 'SI-3')):
            self.sell(100.5, day=(2026, 3, day), number=number)
        # 301.5 rounds once to 302; rounding each row first gave 300.
        self.assertEqual(self.sides(self.ali.person), (302, 0))

        ledger = person_ledger(self.business.id, self.ali.person, side='both')
        self.assertEqual(ledger['closing_balance'], {'receivable': 302, 'payable': 0})
        self.assertEqual(sum(row['naam'] for row in ledger['rows']), 302)
        for row in ledger['rows']:
            self.assertIsInstance(row['naam'], int)

        one = party_ledger(self.business.id, customer=self.ali)
        self.assertEqual(one['closing_balance'], 302)
        self.assertEqual([row['balance'] for row in one['rows']], [100, 201, 302])

    def test_a_fraction_past_zero_makes_no_advance_and_does_not_add_to_the_other_role(self):
        supplier = Supplier.objects.create(business=self.business, person=self.ali.person)
        self.sell(9.6)
        response = self.pay(customer=self.ali.id, amount=10)  # 0.4 over
        self.assertEqual(response.status_code, 201, response.content)
        self.purchase('PI-1', 5.4, (2026, 3, 5), supplier=supplier)
        # The card rounds each role: the customer's -0.4 is nothing, the
        # supplier's 5.4 is 5. Adding the exact parts first would read 6.
        self.assertEqual(self.sides(self.ali.person), (0, 5))

        ledger = person_ledger(self.business.id, self.ali.person, side='both')
        self.assertEqual(ledger['closing_balance'], {'receivable': 0, 'payable': 5})
        self.assertFalse([row for row in ledger['rows'] if 'advance' in row['description']])
