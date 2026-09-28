"""
Tests for the targets engine.

Run with:
    python manage.py test targets --settings=backend.test_settings

The settings override is not optional. sales/migrations/0010 removes five
constraints no earlier migration adds, so a test database cannot be built from
the real migration history.
"""
from datetime import date, datetime, timedelta, timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.test import TestCase

from rest_framework.test import APIClient

from root.models import (
    Business, BusinessConfig, City, Customer, Expense, Supplier, Unit,
)
from root.models import BaseQuerySet
from sales.models import (
    PurchaseInvoice, PurchaseInvoiceItem, SalesInvoice, SalesInvoiceItem,
)
from accounts.models import MoneyAccount, PartyPayment, Transaction

from .models import ManualDataPoint, Target, TargetFilter
from . import utils
from .catalogue import catalogue, resolve_choice_label, resolve_entity_label

User = get_user_model()

TODAY = date(2026, 9, 26)


def make_target(business, **kwargs):
    """A quarter-long purchase_value target unless told otherwise."""
    defaults = dict(
        name='Test target',
        data_point='purchase_value',
        period_type='quarter',
        period_year=2026,
        period_quarter=3,
        target_value=1000.0,
        rule_type='threshold',
    )
    defaults.update(kwargs)
    return Target.objects.create(business=business, **defaults)


class PeriodResolutionTests(TestCase):
    """resolve_period needs no database at all."""

    def resolve(self, **kwargs):
        return utils.resolve_period(Target(**kwargs), today=TODAY)

    def test_month_runs_to_the_last_day(self):
        self.assertEqual(
            self.resolve(period_type='month', period_year=2026, period_month=2),
            (date(2026, 2, 1), date(2026, 2, 28)),
        )

    def test_february_in_a_leap_year_has_29_days(self):
        self.assertEqual(
            self.resolve(period_type='month', period_year=2024, period_month=2),
            (date(2024, 2, 1), date(2024, 2, 29)),
        )

    def test_december_ends_on_the_31st(self):
        self.assertEqual(
            self.resolve(period_type='month', period_year=2026, period_month=12),
            (date(2026, 12, 1), date(2026, 12, 31)),
        )

    def test_month_is_the_whole_month_not_month_to_date(self):
        """
        accounts.utils.month_bounds returns month-to-date. If anybody swaps it
        in here, a monthly target reports 100% on the 1st of every month.
        """
        _, end = self.resolve(
            period_type='month', period_year=2026, period_month=9)
        self.assertEqual(end, date(2026, 9, 30))
        self.assertGreater(end, TODAY)

    def test_all_four_quarters(self):
        expected = {
            1: (date(2026, 1, 1), date(2026, 3, 31)),
            2: (date(2026, 4, 1), date(2026, 6, 30)),
            3: (date(2026, 7, 1), date(2026, 9, 30)),
            4: (date(2026, 10, 1), date(2026, 12, 31)),
        }
        for quarter, window in expected.items():
            with self.subTest(quarter=quarter):
                self.assertEqual(
                    self.resolve(period_type='quarter', period_year=2026,
                                 period_quarter=quarter),
                    window,
                )

    def test_year(self):
        self.assertEqual(
            self.resolve(period_type='year', period_year=2026),
            (date(2026, 1, 1), date(2026, 12, 31)),
        )

    def test_custom_range_is_used_verbatim(self):
        self.assertEqual(
            self.resolve(period_type='custom',
                         date_from=date(2026, 7, 5), date_to=date(2026, 8, 9)),
            (date(2026, 7, 5), date(2026, 8, 9)),
        )

    def test_single_day_custom_range_is_one_day_long(self):
        start, end = self.resolve(period_type='custom',
                                  date_from=TODAY, date_to=TODAY)
        self.assertEqual((end - start).days + 1, 1)

    def test_rolling_one_day_is_today_only(self):
        self.assertEqual(
            self.resolve(period_type='rolling', rolling_days=1),
            (TODAY, TODAY),
        )

    def test_rolling_thirty_days_spans_thirty_inclusive_days(self):
        start, end = self.resolve(period_type='rolling', rolling_days=30)
        self.assertEqual((end - start).days + 1, 30)
        self.assertEqual(start, date(2026, 8, 28))
        self.assertEqual(end, TODAY)

    def test_rolling_differs_from_base_queryset_in_period(self):
        """
        BaseQuerySet.in_period(30) counts back 30 days from this instant on
        created_at, which is a 31-calendar-day span and a different field.
        Asserting the difference keeps anybody from 'simplifying' one into the
        other.
        """
        start, _ = self.resolve(period_type='rolling', rolling_days=30)
        naive_start = TODAY - timedelta(days=30)
        self.assertNotEqual(start, naive_start)
        self.assertEqual((start - naive_start).days, 1)

    def test_unknown_period_type_raises(self):
        with self.assertRaises(ValueError):
            self.resolve(period_type='fortnight')


class EffectiveWindowTests(TestCase):

    def test_window_is_clamped_to_today(self):
        self.assertEqual(
            utils.effective_window(date(2026, 7, 1), date(2026, 9, 30), TODAY),
            (date(2026, 7, 1), TODAY),
        )

    def test_window_is_none_before_the_period_starts(self):
        self.assertIsNone(
            utils.effective_window(date(2026, 10, 1), date(2026, 12, 31), TODAY)
        )

    def test_closed_period_keeps_its_own_end(self):
        self.assertEqual(
            utils.effective_window(date(2026, 4, 1), date(2026, 6, 30), TODAY),
            (date(2026, 4, 1), date(2026, 6, 30)),
        )


class ThresholdEvaluationTests(TestCase):

    def test_not_started_wins_over_everything(self):
        self.assertEqual(
            utils.evaluate_threshold(0, 100, 0, True, started=False),
            utils.STATUS_NOT_STARTED,
        )

    def test_achieved_is_tested_before_missed(self):
        """A target hit in August still reads achieved when opened in September."""
        self.assertEqual(
            utils.evaluate_threshold(120, 100, 1.0, is_open=False, started=True),
            utils.STATUS_ACHIEVED,
        )

    def test_missed_when_the_period_closed_short(self):
        self.assertEqual(
            utils.evaluate_threshold(80, 100, 1.0, is_open=False, started=True),
            utils.STATUS_MISSED,
        )

    def test_on_track_when_ahead_of_the_clock(self):
        self.assertEqual(
            utils.evaluate_threshold(60, 100, 0.5, is_open=True, started=True),
            utils.STATUS_ON_TRACK,
        )

    def test_behind_when_short_of_the_clock(self):
        self.assertEqual(
            utils.evaluate_threshold(40, 100, 0.5, is_open=True, started=True),
            utils.STATUS_BEHIND,
        )

    def test_a_zero_target_is_never_achieved(self):
        self.assertNotEqual(
            utils.evaluate_threshold(0, 0, 0.5, is_open=True, started=True),
            utils.STATUS_ACHIEVED,
        )


class ProgressTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser(
            email='progress@example.com', password='pw-for-tests')
        cls.business = Business.objects.create(
            name='Progress Traders', owner=cls.user, phone='03001234567')

    def test_future_period_reports_not_started_and_issues_no_query(self):
        target = make_target(self.business, period_quarter=4)

        # The resolver must not be reached at all: there is nothing to measure
        # before a period begins, and a business with many future targets
        # should cost nothing to display.
        with self.assertNumQueries(1):   # the prefetch of filters, nothing more
            row = utils.progress(target, today=TODAY)

        self.assertEqual(row['status'], utils.STATUS_NOT_STARTED)
        self.assertEqual(row['actual'], 0)
        self.assertEqual(row['elapsed_days'], 0)
        self.assertEqual(row['remaining_days'], row['total_days'])

    def test_zero_target_value_does_not_divide_by_zero(self):
        target = make_target(self.business, target_value=0)
        row = utils.progress(target, today=TODAY)
        self.assertEqual(row['percentage_achieved'], 0)
        self.assertNotEqual(row['status'], utils.STATUS_ACHIEVED)

    def test_overachievement_reports_above_one_hundred_with_no_gap(self):
        target = make_target(self.business, target_value=100)
        row = utils.progress(target, today=TODAY, measured=140)
        self.assertEqual(row['status'], utils.STATUS_ACHIEVED)
        self.assertEqual(row['percentage_achieved'], 140.0)
        self.assertEqual(row['gap_remaining'], 0)

    def test_rolling_target_is_never_missed(self):
        target = make_target(
            self.business, period_type='rolling', rolling_days=30,
            period_year=None, period_quarter=None, target_value=100)
        row = utils.progress(target, today=TODAY, measured=1)
        self.assertTrue(row['is_open'])
        self.assertNotEqual(row['status'], utils.STATUS_MISSED)

    def test_elapsed_days_never_exceed_the_period(self):
        target = make_target(self.business, period_quarter=1)
        row = utils.progress(target, today=TODAY, measured=0)
        self.assertEqual(row['elapsed_days'], row['total_days'])
        self.assertEqual(row['remaining_days'], 0)

    def test_labels_travel_with_the_figure(self):
        target = make_target(self.business)
        row = utils.progress(target, today=TODAY, measured=0)
        self.assertEqual(row['period_label'], 'Q3 2026')
        self.assertEqual(row['scope_label'], 'Whole business')
        self.assertEqual(row['date_basis_label'], 'Invoice date')
        self.assertIn('gross', row['returns_treatment'])
        self.assertEqual(row['unit'], 'amount')


class MeasurementTests(TestCase):
    """The resolvers, through the engine, against real rows."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser(
            email='measure@example.com', password='pw-for-tests')
        cls.business = Business.objects.create(
            name='Measure Traders', owner=cls.user, phone='03001234567')
        cls.other = Business.objects.create(
            name='Other Traders', owner=cls.user, phone='03007654321')

        cls.city = City.objects.create(name='Hyderabad', postal_code='71000')
        cls.customer = Customer.objects.create(
            name='Ali', business=cls.business, city=cls.city)
        cls.supplier = Supplier.objects.create(
            business=cls.business, name='Jotun')
        cls.other_supplier = Supplier.objects.create(
            business=cls.business, name='Berger')

    def sales_invoice(self, total, status='C', business=None, day=15):
        return SalesInvoice.objects.create(
            business=business or self.business,
            customer=self.customer,
            date_issued=datetime(2026, 8, day, 12, 0, tzinfo=dt_timezone.utc),
            status=status,
            total=total,
            created_by=self.user,
        )

    def purchase_invoice(self, total, status='R', supplier=None, business=None):
        return PurchaseInvoice.objects.create(
            business=business or self.business,
            supplier=supplier or self.supplier,
            date_issued=datetime(2026, 8, 15, 12, 0, tzinfo=dt_timezone.utc),
            status=status,
            total=total,
            created_by=self.user,
        )

    def q3(self, **kwargs):
        return make_target(self.business, **kwargs)

    def measure(self, target):
        date_from, date_to = utils.resolve_period(target, today=TODAY)
        window = utils.effective_window(date_from, date_to, TODAY)
        return utils.measure(target, *window)

    # --- cancellations ---------------------------------------------------

    def test_cancelled_sales_invoice_is_excluded_but_completed_is_counted(self):
        """
        'C' means COMPLETED on a sales invoice and CANCELLED on a purchase one.
        This is the test that catches a copy-pasted constant.
        """
        self.sales_invoice(1000, status='C')     # completed — counts
        self.sales_invoice(500, status='X')      # cancelled — does not
        target = self.q3(data_point='sales_revenue')
        self.assertEqual(self.measure(target), 1000)

    def test_cancelled_purchase_invoice_is_excluded(self):
        self.purchase_invoice(1000, status='R')
        self.purchase_invoice(500, status='C')   # cancelled on purchases
        target = self.q3(data_point='purchase_value')
        self.assertEqual(self.measure(target), 1000)

    def test_draft_invoices_are_counted(self):
        self.sales_invoice(700, status='D')
        target = self.q3(data_point='sales_revenue')
        self.assertEqual(self.measure(target), 700)

    # --- net of returns --------------------------------------------------

    def test_items_sold_subtracts_returned_quantity(self):
        invoice = self.sales_invoice(1000)
        SalesInvoiceItem.objects.create(
            business=self.business, sales_invoice=invoice,
            quantity=10, returned_quantity=3, unit_price=10)
        target = self.q3(data_point='items_sold')
        self.assertEqual(self.measure(target), 7)

    def test_a_fully_returned_line_nets_to_zero_without_being_dropped(self):
        invoice = self.sales_invoice(1000)
        SalesInvoiceItem.objects.create(
            business=self.business, sales_invoice=invoice,
            quantity=5, returned_quantity=5, unit_price=10)
        SalesInvoiceItem.objects.create(
            business=self.business, sales_invoice=invoice,
            quantity=4, returned_quantity=0, unit_price=10)
        target = self.q3(data_point='items_sold')
        self.assertEqual(self.measure(target), 4)

    # --- cash ------------------------------------------------------------

    def cash(self, amount, type_, status='C', day=15):
        account = MoneyAccount.objects.get_default_account(self.business.id)
        return Transaction.objects.create(
            business=self.business, account=account, type=type_,
            amount=amount, date=date(2026, 8, day), status=status)

    def test_cash_received_nets_refunds_and_ignores_uncleared(self):
        self.cash(1000, 'sale_payment')
        self.cash(500, 'customer_receipt')
        self.cash(200, 'sales_return_refund')
        self.cash(9999, 'sale_payment', status='PEN')   # cheque not cleared
        target = self.q3(data_point='cash_received')
        self.assertEqual(self.measure(target), 1300)

    def test_a_refund_only_period_is_negative_not_none(self):
        """Sum over no rows is NULL, and NULL minus 0 is NULL. Coalesce guards it."""
        self.cash(200, 'sales_return_refund')
        target = self.q3(data_point='cash_received')
        self.assertEqual(self.measure(target), -200)

    def test_an_empty_period_measures_zero_not_none(self):
        target = self.q3(data_point='cash_received')
        self.assertEqual(self.measure(target), 0)

    # --- filters ---------------------------------------------------------

    def test_a_supplier_filter_narrows_the_figure(self):
        self.purchase_invoice(1000, supplier=self.supplier)
        self.purchase_invoice(400, supplier=self.other_supplier)
        target = self.q3(data_point='purchase_value')
        self.assertEqual(self.measure(target), 1400)

        TargetFilter.objects.create(
            target=target, dimension='supplier',
            value_id=self.supplier.id, label='Jotun')
        target.refresh_from_db()
        self.assertEqual(self.measure(target), 1000)

    def test_a_filter_on_a_deleted_supplier_measures_zero_and_keeps_its_label(self):
        self.purchase_invoice(1000, supplier=self.other_supplier)
        target = self.q3(data_point='purchase_value')
        TargetFilter.objects.create(
            target=target, dimension='supplier',
            value_id=self.supplier.id, label='Jotun')

        self.supplier.delete()

        # The target survives, the card still reads "Jotun", and the figure is
        # an honest zero rather than a crash or a silent widening to 1400.
        target.refresh_from_db()
        self.assertEqual(self.measure(target), 0)
        self.assertEqual(target.filters.first().label, 'Jotun')
        row = utils.progress(target, today=TODAY)
        self.assertEqual(row['filters'][0]['label'], 'Jotun')

    def test_a_subject_narrows_the_figure(self):
        self.purchase_invoice(1000, supplier=self.supplier)
        self.purchase_invoice(400, supplier=self.other_supplier)
        target = self.q3(
            data_point='purchase_value', scope_type='subject',
            subject_type='supplier', subject_id=self.supplier.id,
            subject_label='Jotun')
        self.assertEqual(self.measure(target), 1000)
        self.assertEqual(
            utils.progress(target, today=TODAY)['scope_label'], 'Jotun')

    # --- tenancy ---------------------------------------------------------

    def test_a_target_never_measures_another_business(self):
        self.sales_invoice(1000, business=self.business)
        other_customer = Customer.objects.create(
            name='Zed', business=self.other, city=self.city)
        SalesInvoice.objects.create(
            business=self.other, customer=other_customer,
            date_issued=datetime(2026, 8, 15, 12, 0, tzinfo=dt_timezone.utc),
            status='C', total=5000, created_by=self.user)

        target = self.q3(data_point='sales_revenue')
        self.assertEqual(self.measure(target), 1000)

    # --- expenses --------------------------------------------------------

    def test_expenses_are_dated_on_created_at(self):
        expense = Expense.objects.create(
            business=self.business, name='Bijli', amount=750)
        # created_at is auto_now_add, so the row lands today. A Q3 target that
        # contains today therefore sees it.
        Expense.objects.filter(pk=expense.pk).update(
            created_at=datetime(2026, 8, 15, 12, 0, tzinfo=dt_timezone.utc))
        target = self.q3(data_point='expenses')
        self.assertEqual(self.measure(target), 750)


class TimezoneBoundaryTests(TestCase):
    """
    TIME_ZONE is Asia/Karachi, five hours ahead of UTC. A sale entered at 02:00
    on 1 October is stored as 2026-09-30T21:00Z, and it belongs to October.

    This is the single easiest thing in the module to get wrong: a naive UTC
    truncation puts it in September, and the two database backends reach the
    answer by different routes — SQLite through a Python function Django
    registers, Postgres through AT TIME ZONE.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser(
            email='tz@example.com', password='pw-for-tests')
        cls.business = Business.objects.create(
            name='Timezone Traders', owner=cls.user, phone='03001234567')
        cls.city = City.objects.create(name='Karachi', postal_code='74000')
        cls.customer = Customer.objects.create(
            name='Ali', business=cls.business, city=cls.city)

    def invoice_at(self, when, total):
        return SalesInvoice.objects.create(
            business=self.business, customer=self.customer,
            date_issued=when, status='C', total=total, created_by=self.user)

    def month(self, month):
        target = make_target(
            self.business, data_point='sales_revenue', period_type='month',
            period_year=2026, period_month=month, period_quarter=None)
        date_from, date_to = utils.resolve_period(target, today=date(2026, 11, 1))
        return utils.measure(target, date_from, date_to)

    def test_a_late_night_karachi_sale_belongs_to_the_next_month(self):
        # 02:00 on 1 October, Karachi
        self.invoice_at(datetime(2026, 9, 30, 21, 0, tzinfo=dt_timezone.utc), 111)
        self.assertEqual(self.month(10), 111)
        self.assertEqual(self.month(9), 0)

    def test_a_late_evening_karachi_sale_stays_in_its_own_month(self):
        # 23:59 on 30 September, Karachi
        self.invoice_at(datetime(2026, 9, 30, 18, 59, tzinfo=dt_timezone.utc), 222)
        self.assertEqual(self.month(9), 222)
        self.assertEqual(self.month(10), 0)


class DashboardTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser(
            email='dashboard@example.com', password='pw-for-tests')
        cls.business = Business.objects.create(
            name='Dashboard Traders', owner=cls.user, phone='03001234567')

    def test_identical_shapes_are_measured_once(self):
        """
        Ten targets over three distinct questions must ask the database three
        times. Without this the dashboard is one aggregate per target, and the
        project has no caching to fall back on.
        """
        for index in range(10):
            data_point = ['sales_revenue', 'purchase_value', 'expenses'][index % 3]
            make_target(self.business, name=f'T{index}', data_point=data_point,
                        target_value=100 + index)

        targets = list(Target.objects.for_business(self.business.id)
                       .prefetch_related('filters'))
        calls = []
        original = utils.measure

        def counting_measure(target, date_from, date_to):
            calls.append(utils.shape_key(target, date_from, date_to))
            return original(target, date_from, date_to)

        utils.measure = counting_measure
        try:
            rows = utils.dashboard_progress(targets, today=TODAY)
        finally:
            utils.measure = original

        self.assertEqual(len(rows), 10)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(set(calls)), 3)

    def test_different_filters_are_different_shapes(self):
        first = make_target(self.business, name='A')
        second = make_target(self.business, name='B')
        TargetFilter.objects.create(
            target=second, dimension='supplier', value_id=7, label='Jotun')

        date_from, date_to = utils.resolve_period(first, today=TODAY)
        self.assertNotEqual(
            utils.shape_key(first, date_from, date_to),
            utils.shape_key(second, date_from, date_to),
        )

    def test_future_targets_cost_no_measurement(self):
        make_target(self.business, period_quarter=4)
        targets = list(Target.objects.for_business(self.business.id)
                       .prefetch_related('filters'))

        calls = []
        original = utils.measure
        utils.measure = lambda *args: calls.append(args) or 0
        try:
            rows = utils.dashboard_progress(targets, today=TODAY)
        finally:
            utils.measure = original

        self.assertEqual(calls, [])
        self.assertEqual(rows[0]['status'], utils.STATUS_NOT_STARTED)

    def test_summarise_counts_every_status(self):
        rows = [
            {'status': utils.STATUS_ACHIEVED, 'is_open': True},
            {'status': utils.STATUS_BEHIND, 'is_open': True},
            {'status': utils.STATUS_MISSED, 'is_open': False},
        ]
        summary = utils.summarise(rows)
        self.assertEqual(summary['total'], 3)
        self.assertEqual(summary['open'], 2)
        self.assertEqual(summary[utils.STATUS_ACHIEVED], 1)
        self.assertEqual(summary[utils.STATUS_MISSED], 1)


class CatalogueTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser(
            email='catalogue@example.com', password='pw-for-tests')
        cls.business = Business.objects.create(
            name='Catalogue Traders', owner=cls.user, phone='03001234567')
        cls.city = City.objects.create(name='Sukkur', postal_code='65200')
        cls.customer = Customer.objects.create(
            name='Ali', business=cls.business, city=cls.city)
        cls.other = Business.objects.create(
            name='Elsewhere', owner=cls.user, phone='03009999999')
        cls.foreign_customer = Customer.objects.create(
            name='Foreign', business=cls.other, city=cls.city)

    def test_every_entry_declares_how_returns_are_treated(self):
        for code, spec in catalogue().items():
            with self.subTest(code=code):
                self.assertTrue(spec.returns_treatment)
                self.assertTrue(spec.date_basis_label)
                self.assertIn(spec.unit, ('amount', 'count'))

    def test_an_in_tenant_entity_resolves_to_its_name(self):
        self.assertEqual(
            resolve_entity_label('customer', self.customer.id, self.business),
            'Ali',
        )

    def test_a_foreign_entity_does_not_resolve(self):
        self.assertIsNone(
            resolve_entity_label('customer', self.foreign_customer.id,
                                 self.business)
        )

    def test_the_owner_resolves_as_entered_by(self):
        self.assertEqual(
            resolve_entity_label('entered_by', self.user.id, self.business),
            self.user.email,
        )

    def test_sales_statuses_exclude_cancelled_and_purchase_statuses_differ(self):
        # 'C' is offered on a sales invoice (COMPLETED) and withheld on a
        # purchase one (CANCELLED).
        self.assertIsNotNone(
            resolve_choice_label('invoice_status', 'C', 'sales_revenue'))
        self.assertIsNone(
            resolve_choice_label('invoice_status', 'X', 'sales_revenue'))
        self.assertIsNone(
            resolve_choice_label('invoice_status', 'C', 'purchase_value'))
        self.assertIsNotNone(
            resolve_choice_label('invoice_status', 'R', 'purchase_value'))

    def test_an_unknown_choice_does_not_resolve(self):
        self.assertIsNone(
            resolve_choice_label('payment_method', 'barter'))


class ManualDataPointTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser(
            email='manual@example.com', password='pw-for-tests')
        cls.business = Business.objects.create(
            name='Manual Traders', owner=cls.user, phone='03001234567')

    def test_a_manual_data_point_is_not_in_the_catalogue(self):
        """
        Nothing may make a typed-in figure measurable. There is no foreign key
        and no catalogue entry, so no code path exists.
        """
        point = ManualDataPoint.objects.create(
            business=self.business, name="Jotun's own figure", value=4800000,
            value_type='amount', as_of_date=date(2026, 9, 20))
        self.assertNotIn(point.name, catalogue())
        self.assertFalse(
            [field for field in ManualDataPoint._meta.get_fields()
             if field.related_model is Target]
        )


class TargetAPITests(TestCase):
    """The serializers and viewsets, through the real URL routing."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_superuser(
            email='api-owner@example.com', password='pw-for-tests')
        cls.business = Business.objects.create(
            name='API Traders', owner=cls.owner, phone='03001234567',
            is_active=True)
        # BusinessConfig is not created by a signal, so the add-on flag has to
        # be set up explicitly. That is also what makes the gate testable.
        cls.config = BusinessConfig.objects.create(
            business=cls.business, targets=True)

        cls.city = City.objects.create(name='Larkana', postal_code='77150')
        cls.customer = Customer.objects.create(
            name='Ali', business=cls.business, city=cls.city)
        cls.supplier = Supplier.objects.create(
            business=cls.business, name='Jotun')

        cls.outsider = User.objects.create_superuser(
            email='api-outsider@example.com', password='pw-for-tests')
        cls.other_business = Business.objects.create(
            name='Rival Traders', owner=cls.outsider, phone='03007654321',
            is_active=True)

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    def payload(self, **overrides):
        body = {
            'name': 'Jotun quarterly',
            'data_point': 'purchase_value',
            'period_type': 'quarter',
            'period_year': 2026,
            'period_quarter': 3,
            'target_value': 5000000,
            'rule_type': 'threshold',
            'outcome': '3% quarterly discount',
        }
        body.update(overrides)
        return body

    # --- the gate --------------------------------------------------------

    def test_list_is_empty_and_kpis_are_forbidden_without_the_add_on(self):
        self.config.targets = False
        self.config.save(update_fields=['targets'])

        self.assertEqual(self.client.get('/targets/').json(), [])
        self.assertEqual(
            self.client.get('/target-kpis/summary/').status_code, 403)
        self.assertEqual(
            self.client.get('/targets/catalogue/').status_code, 403)

    def test_writes_are_refused_without_the_add_on(self):
        """
        Gating get_queryset alone is not enough: ModelViewSet.create never calls
        it, so before HasTargetsAccess existed a business without the add-on
        could create targets it was not allowed to read back.
        """
        target = Target.objects.create(
            business=self.business, name='Existing',
            data_point='sales_revenue', period_type='year', period_year=2026,
            target_value=100)

        self.config.targets = False
        self.config.save(update_fields=['targets'])

        self.assertEqual(
            self.client.post('/targets/', self.payload(), format='json')
            .status_code, 403)
        self.assertEqual(
            self.client.put(f'/targets/{target.id}/', self.payload(),
                            format='json').status_code, 403)
        self.assertEqual(
            self.client.delete(f'/targets/{target.id}/').status_code, 403)
        self.assertEqual(
            self.client.post('/targets/bulk-delete/',
                             {'target_ids': [target.id]}, format='json')
            .status_code, 403)
        self.assertEqual(
            self.client.post('/manual-data-points/',
                             {'name': 'x', 'value': 1}, format='json')
            .status_code, 403)

        self.assertTrue(Target.objects.filter(id=target.id).exists())

    def test_every_endpoint_requires_authentication(self):
        """
        permission_classes REPLACES DEFAULT_PERMISSION_CLASSES rather than
        adding to it, so naming only the targets gate on these viewsets left
        every one of them open to anonymous callers. IsAuthenticated has to be
        listed explicitly alongside it.
        """
        anon = APIClient()
        paths = [
            '/targets/', '/manual-data-points/', '/targets/catalogue/',
            '/target-kpis/dashboard/', '/target-kpis/summary/',
            '/target-kpis/progress/',
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(anon.get(path).status_code, 401)

        self.assertEqual(
            anon.post('/targets/', self.payload(), format='json').status_code,
            401)

    def test_a_create_response_carries_the_new_id(self):
        """Without it a client cannot act on what it just made."""
        response = self.client.post('/targets/', self.payload(), format='json')
        self.assertEqual(response.status_code, 201)
        self.assertIn('id', response.json())
        self.assertEqual(
            response.json()['id'],
            Target.objects.get(business=self.business).id,
        )

    def test_the_catalogue_lists_nine_measures_each_explaining_returns(self):
        response = self.client.get('/targets/catalogue/')
        self.assertEqual(response.status_code, 200)
        entries = response.json()['catalogue']
        self.assertEqual(len(entries), 9)
        for entry in entries:
            self.assertTrue(entry['returns_treatment'])
            self.assertTrue(entry['date_basis_label'])

    # --- creating --------------------------------------------------------

    def test_a_target_is_created_with_its_filter_label_captured(self):
        body = self.payload(filters=[
            {'dimension': 'supplier', 'value_id': self.supplier.id}])
        response = self.client.post('/targets/', body, format='json')
        self.assertEqual(response.status_code, 201)

        target = Target.objects.get(business=self.business)
        row = target.filters.get()
        self.assertEqual(row.dimension, 'supplier')
        self.assertEqual(row.value_id, self.supplier.id)
        # Resolved server-side, never taken from the client.
        self.assertEqual(row.label, 'Jotun')

    def test_an_unimplemented_rule_is_refused(self):
        response = self.client.post(
            '/targets/', self.payload(rule_type='slab'), format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('rule_type', response.json())

    def test_a_target_of_zero_is_refused(self):
        response = self.client.post(
            '/targets/', self.payload(target_value=0), format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('target_value', response.json())

    def test_a_backwards_custom_range_is_refused(self):
        response = self.client.post('/targets/', self.payload(
            period_type='custom', period_year=None, period_quarter=None,
            date_from='2026-09-30', date_to='2026-09-01'), format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('date_to', response.json())

    def test_a_filter_the_data_point_does_not_offer_is_refused(self):
        # purchase_value has no customer dimension.
        response = self.client.post('/targets/', self.payload(filters=[
            {'dimension': 'customer', 'value_id': self.customer.id}
        ]), format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('filters', response.json())

    def test_a_filter_on_another_business_entity_is_refused(self):
        foreign = Supplier.objects.create(
            business=self.other_business, name='Berger')
        response = self.client.post('/targets/', self.payload(filters=[
            {'dimension': 'supplier', 'value_id': foreign.id}
        ]), format='json')
        self.assertEqual(response.status_code, 400)

    def test_a_subject_type_the_data_point_does_not_offer_is_refused(self):
        response = self.client.post('/targets/', self.payload(
            scope_type='subject', subject_type='customer',
            subject_id=self.customer.id), format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('subject_type', response.json())

    def test_expenses_cannot_be_scoped_to_a_subject(self):
        """Expense has no created_by, so it can only ever be business-wide."""
        response = self.client.post('/targets/', self.payload(
            data_point='expenses', scope_type='subject',
            subject_type='employee', subject_id=self.owner.id), format='json')
        self.assertEqual(response.status_code, 400)

    # --- editing ---------------------------------------------------------

    def create_target(self, **overrides):
        response = self.client.post(
            '/targets/', self.payload(**overrides), format='json')
        self.assertEqual(response.status_code, 201, response.json())
        return Target.objects.get(business=self.business)

    def test_every_attribute_of_an_open_target_can_be_changed(self):
        target = self.create_target()
        response = self.client.put(f'/targets/{target.id}/', self.payload(
            name='Rewritten', data_point='sales_revenue',
            period_type='month', period_year=2026, period_month=9,
            period_quarter=None, target_value=800000,
            filters=[{'dimension': 'customer', 'value_id': self.customer.id}],
        ), format='json')
        self.assertEqual(response.status_code, 200, response.json())

        target.refresh_from_db()
        self.assertEqual(target.name, 'Rewritten')
        self.assertEqual(target.data_point, 'sales_revenue')
        self.assertEqual(target.period_type, 'month')
        self.assertEqual(target.filters.get().label, 'Ali')

    def test_switching_period_type_clears_the_fields_that_no_longer_apply(self):
        target = self.create_target(
            period_type='custom', period_year=None, period_quarter=None,
            date_from='2026-07-01', date_to='2026-09-30')
        self.assertIsNotNone(target.date_to)

        self.client.put(f'/targets/{target.id}/', self.payload(
            period_type='month', period_year=2026, period_month=9,
            period_quarter=None), format='json')

        target.refresh_from_db()
        # A stale date_to would silently describe a window nobody asked for.
        self.assertIsNone(target.date_to)
        self.assertIsNone(target.date_from)
        self.assertEqual(target.period_month, 9)

    def test_an_ended_target_refuses_edits_but_allows_duplication(self):
        target = self.create_target(
            period_type='custom', period_year=None, period_quarter=None,
            date_from='2020-01-01', date_to='2020-03-31')

        response = self.client.put(f'/targets/{target.id}/', self.payload(
            period_type='custom', period_year=None, period_quarter=None,
            date_from='2020-01-01', date_to='2020-03-31',
            name='Cannot rename'), format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('ended', str(response.json()).lower())

        duplicated = self.client.post(f'/targets/{target.id}/duplicate/')
        self.assertEqual(duplicated.status_code, 201)
        self.assertEqual(
            Target.objects.filter(business=self.business).count(), 2)

    def test_duplicating_copies_the_filters(self):
        target = self.create_target(filters=[
            {'dimension': 'supplier', 'value_id': self.supplier.id}])
        self.client.post(f'/targets/{target.id}/duplicate/')
        copy = Target.objects.exclude(id=target.id).get()
        self.assertEqual(copy.name, 'Jotun quarterly (copy)')
        self.assertEqual(copy.filters.get().label, 'Jotun')

    # --- deleting --------------------------------------------------------

    def test_bulk_delete_cannot_reach_another_business(self):
        mine = self.create_target()
        theirs = Target.objects.create(
            business=self.other_business, name='Not mine',
            data_point='sales_revenue', period_type='year', period_year=2026,
            target_value=100)

        response = self.client.post(
            '/targets/bulk-delete/',
            {'target_ids': [mine.id, theirs.id]}, format='json')
        self.assertEqual(response.status_code, 200)

        self.assertFalse(Target.objects.filter(id=mine.id).exists())
        self.assertTrue(Target.objects.filter(id=theirs.id).exists())

    # --- reading ---------------------------------------------------------

    def test_the_dashboard_and_summary_agree(self):
        self.create_target()
        dashboard = self.client.get(
            '/target-kpis/dashboard/').json()['dashboard']
        summary = self.client.get('/target-kpis/summary/').json()['summary']
        self.assertEqual(len(dashboard), 1)
        self.assertEqual(summary['total'], 1)
        self.assertIn('actual', dashboard[0])
        self.assertIn('percentage_achieved', dashboard[0])
        self.assertIn('returns_treatment', dashboard[0])
        # Needed to interleave targets with manual data points, newest first.
        self.assertIsNotNone(dashboard[0]['created_at'])

    def test_progress_needs_a_target_id_and_stays_in_the_tenant(self):
        self.assertEqual(
            self.client.get('/target-kpis/progress/').status_code, 400)

        theirs = Target.objects.create(
            business=self.other_business, name='Not mine',
            data_point='sales_revenue', period_type='year', period_year=2026,
            target_value=100)
        self.assertEqual(
            self.client.get(f'/target-kpis/progress/?target_id={theirs.id}')
            .status_code, 404)

    def test_a_retrieve_carries_the_resolved_window_but_no_figure(self):
        target = self.create_target()
        body = self.client.get(f'/targets/{target.id}/').json()
        self.assertEqual(body['period_label'], 'Q3 2026')
        self.assertEqual(body['date_from_resolved'], '2026-07-01')
        self.assertEqual(body['date_to_resolved'], '2026-09-30')
        self.assertNotIn('actual', body)


class ManualDataPointAPITests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_superuser(
            email='manual-api@example.com', password='pw-for-tests')
        cls.business = Business.objects.create(
            name='Manual API Traders', owner=cls.owner, phone='03001234567',
            is_active=True)
        BusinessConfig.objects.create(business=cls.business, targets=True)

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    def test_a_manual_data_point_round_trips(self):
        response = self.client.post('/manual-data-points/', {
            'name': 'Supplier statement figure',
            'value': 4800000,
            'value_type': 'amount',
            'as_of_date': '2026-09-20',
        }, format='json')
        self.assertEqual(response.status_code, 201)

        listed = self.client.get('/manual-data-points/').json()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]['value'], 4800000)

    def test_bulk_delete_reads_data_point_ids(self):
        point = ManualDataPoint.objects.create(
            business=self.business, name='Scratch', value=1)
        response = self.client.post(
            '/manual-data-points/bulk-delete/',
            {'data_point_ids': [point.id]}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ManualDataPoint.objects.filter(id=point.id).exists())
