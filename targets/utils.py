"""
Period resolution, arithmetic and rule evaluation for the targets module.

There is no ORM here. Every figure comes from a manager method named by the
catalogue; this module decides which window to ask about, then compares what
comes back against the number the owner set.
"""
from calendar import monthrange
from datetime import date, timedelta

from django.utils import timezone

from root.utils import local_date

from .catalogue import get_spec, subject_kwarg, dimension_kwarg


MODULE = 'targets'

STATUS_NOT_STARTED = 'not_started'
STATUS_ON_TRACK = 'on_track'
STATUS_BEHIND = 'behind'
STATUS_ACHIEVED = 'achieved'
STATUS_MISSED = 'missed'

_MONTH_NAMES = [
    '', 'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December',
]


def has_targets_access(request, business):
    """
    Target data is invisible without targets permission.

    Admins are gated by the paid add-on flag on BusinessConfig; employees by
    their own permission matrix. Mirrors accounts.utils.has_accounting_access.
    """
    user = request.user

    if not user or not user.is_authenticated or not business:
        return False

    if user.role == 'admin':
        try:
            return bool(business.config.targets)
        except Exception as error:
            print(error)
            return False

    try:
        employee = user.emp_records.filter(business_id=business.id).first()
        if not employee:
            return False
        return bool(employee.access.permissions.get(MODULE, {}).get('view'))
    except Exception as error:
        print(error)
        return False


### Periods ---------------------------------------------------------------

def resolve_period(target, today=None):
    """
    The target's stored period fields as two absolute dates, inclusive.

    today comes from local_date(), the project's only correct conversion from
    an aware datetime to a calendar date in Asia/Karachi. date.today() would
    read the server's timezone and timezone.now().date() would read UTC; both
    put a late-evening record in the wrong day.

    Do not reach for accounts.utils.month_bounds here. That returns month-to-
    DATE, so a monthly target built on it would report 100% every 1st.
    """
    today = today or local_date()

    period_type = target.period_type

    if period_type == 'month':
        year, month = target.period_year, target.period_month
        return date(year, month, 1), date(year, month, monthrange(year, month)[1])

    if period_type == 'quarter':
        year = target.period_year
        start_month = 3 * (target.period_quarter - 1) + 1
        end_month = start_month + 2
        return (date(year, start_month, 1),
                date(year, end_month, monthrange(year, end_month)[1]))

    if period_type == 'year':
        year = target.period_year
        return date(year, 1, 1), date(year, 12, 31)

    if period_type == 'custom':
        return target.date_from, target.date_to

    if period_type == 'rolling':
        # Inclusive of today, so 30 days means 30 calendar days and not 31.
        # Deliberately unlike BaseQuerySet.in_period(days), which counts back
        # from this instant on created_at.
        return today - timedelta(days=target.rolling_days - 1), today

    raise ValueError(f'unknown period type: {period_type}')


def effective_window(date_from, date_to, today):
    """
    The part of the period that has actually happened, or None when it has not
    begun. None is what lets progress() skip the query entirely.
    """
    if today < date_from:
        return None
    return date_from, min(date_to, today)


def period_label(target, date_from, date_to):
    period_type = target.period_type

    if period_type == 'month':
        return f'{_MONTH_NAMES[target.period_month]} {target.period_year}'
    if period_type == 'quarter':
        return f'Q{target.period_quarter} {target.period_year}'
    if period_type == 'year':
        return str(target.period_year)
    if period_type == 'rolling':
        return f'Last {target.rolling_days} days'
    return (f'{date_from.strftime("%d %b %Y")} to '
            f'{date_to.strftime("%d %b %Y")}')


### Measurement -----------------------------------------------------------

def resolver_kwargs(target):
    """
    The stored filters and subject as keyword arguments for the resolver.

    The only place a dimension name becomes a keyword argument name. Nothing
    here comes from the request: dimensions were validated against the data
    point when the target was saved.
    """
    kwargs = {}

    for row in target.filters.all():
        kwarg = dimension_kwarg(row.dimension)
        if not kwarg:
            continue
        kwargs[kwarg] = row.value_id if row.value_id is not None else row.value_text

    if target.scope_type == 'subject' and target.subject_id is not None:
        kwarg = subject_kwarg(target.subject_type)
        if kwarg:
            kwargs[kwarg] = target.subject_id

    return kwargs


def measure(target, date_from, date_to):
    """Hand the window and the filters to the manager that owns the number."""
    spec = get_spec(target.data_point)
    if not spec:
        return 0

    return spec.resolver(
        target.business_id, date_from, date_to, **resolver_kwargs(target)
    )


def evaluate_threshold(actual, target_value, elapsed_fraction, is_open,
                       started):
    """
    Which of the five states the target is in.

    The order is the logic. achieved is tested before missed so a target hit in
    August still reads achieved when the page is opened in September, and a
    rolling window is never missed because it never ends.
    """
    if not started:
        return STATUS_NOT_STARTED
    if target_value > 0 and actual >= target_value:
        return STATUS_ACHIEVED
    if not is_open:
        return STATUS_MISSED
    if actual >= target_value * elapsed_fraction:
        return STATUS_ON_TRACK
    return STATUS_BEHIND


def progress(target, today=None, measured=None):
    """
    Everything one card needs: the figure, the arithmetic around it, the status,
    and the labels that make the figure readable.

    measured lets dashboard_progress() supply a number it has already worked out
    for an identically shaped target, so the resolver is not called twice.
    """
    today = today or local_date()
    spec = get_spec(target.data_point)

    date_from, date_to = resolve_period(target, today)
    window = effective_window(date_from, date_to, today)
    started = window is not None

    total_days = (date_to - date_from).days + 1

    if not started:
        actual = 0
        elapsed_days = 0
    else:
        actual = measured if measured is not None else measure(target, *window)
        elapsed_days = min((today - date_from).days + 1, total_days)

    remaining_days = max(total_days - elapsed_days, 0)
    elapsed_fraction = (elapsed_days / total_days) if total_days else 0

    target_value = target.target_value or 0
    # A target of zero can only come from a hand-edited row; the serializer
    # requires more. Guarding here stops it becoming a 500.
    percentage = round(actual / target_value * 100, 1) if target_value else 0
    gap_remaining = max(target_value - actual, 0)

    is_open = target.period_type == 'rolling' or today <= date_to

    status = evaluate_threshold(
        actual, target_value, elapsed_fraction, is_open, started)

    if target.scope_type == 'subject':
        scope_label = target.subject_label or 'One subject'
    else:
        scope_label = 'Whole business'

    return {
        'id': target.id,
        'name': target.name,
        # The dashboard interleaves targets with manual data points, newest
        # first, and the client cannot order two lists without a shared key.
        # localtime so this reads the same way DRF serialises the data point's
        # created_at; both carry an offset, so either sorts correctly, but a
        # payload that mixes UTC and Karachi invites a misreading.
        'created_at': (timezone.localtime(target.created_at).isoformat()
                       if target.created_at else None),
        'data_point': target.data_point,
        'data_point_label': spec.label if spec else target.data_point,
        'unit': spec.unit if spec else 'count',
        'date_basis_label': spec.date_basis_label if spec else '',
        'returns_treatment': spec.returns_treatment if spec else '',
        'period_type': target.period_type,
        'period_label': period_label(target, date_from, date_to),
        'date_from': date_from.isoformat(),
        'date_to': date_to.isoformat(),
        'scope_label': scope_label,
        'filters': [
            {'dimension': row.dimension, 'label': row.label}
            for row in target.filters.all()
        ],
        'target_value': target_value,
        'actual': actual,
        'percentage_achieved': percentage,
        'gap_remaining': gap_remaining,
        'total_days': total_days,
        'elapsed_days': elapsed_days,
        'remaining_days': remaining_days,
        'status': status,
        'is_open': is_open,
        'rule_type': target.rule_type,
        'outcome': target.outcome,
    }


### The dashboard ---------------------------------------------------------

def shape_key(target, date_from, date_to):
    """
    What makes two targets ask the database the same question.

    Same data point, same window, same subject, same filters — so the same
    number. Nothing about the target value or the rule belongs here, because
    those change the verdict and not the measurement.
    """
    filters = tuple(sorted(
        (row.dimension, row.value_id, row.value_text)
        for row in target.filters.all()
    ))
    return (
        target.data_point, date_from, date_to,
        target.scope_type, target.subject_type, target.subject_id,
        filters,
    )


def dashboard_progress(targets, today=None):
    """
    A card per target, asking the database once per distinct question.

    Ten business-wide monthly targets over four data points hit the database
    four times, not ten. This is memoisation within one request, not a cache:
    an identical shape cannot have a different answer, so there is nothing to
    invalidate.
    """
    today = today or local_date()
    seen = {}
    rows = []

    for target in targets:
        date_from, date_to = resolve_period(target, today)
        window = effective_window(date_from, date_to, today)

        measured = None
        if window is not None:
            key = shape_key(target, *window)
            if key not in seen:
                seen[key] = measure(target, *window)
            measured = seen[key]

        rows.append(progress(target, today=today, measured=measured))

    return rows


def summarise(rows):
    """The counts behind the four cards at the top of the dashboard."""
    summary = {
        'total': len(rows),
        'open': 0,
        STATUS_NOT_STARTED: 0,
        STATUS_ON_TRACK: 0,
        STATUS_BEHIND: 0,
        STATUS_ACHIEVED: 0,
        STATUS_MISSED: 0,
    }

    for row in rows:
        summary[row['status']] = summary.get(row['status'], 0) + 1
        if row['is_open']:
            summary['open'] += 1

    return summary
