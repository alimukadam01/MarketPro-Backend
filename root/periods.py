"""
Reporting periods for metric cards, charts and list tables.

One place decides what "this week" means, so a card, the table under it and
the chart beside it can never disagree about which days they cover.

  today    - the business's today (Asia/Karachi, via timezone.localdate)
  week     - Saturday to today; the shop week starts on Saturday
  month    - the 1st of this month to today
  quarter  - the start of this calendar quarter (Jan, Apr, Jul, Oct) to today
  year     - 1 January to today
  custom   - date_from to date_to, both given, inclusive
  all      - no bounds at all

Every period ends today, never at the end of the month or quarter: a figure
for days that have not happened yet is a figure that can only be zero.
"""
from calendar import monthrange
from datetime import date, datetime, timedelta

from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework.exceptions import ValidationError

PERIOD_PARAM = 'period'
DATE_FROM_PARAM = 'date_from'
DATE_TO_PARAM = 'date_to'
PERIODS = ('all', 'today', 'week', 'month', 'quarter', 'year', 'custom')

# Python's weekday(): Monday is 0, so Saturday is 5.
WEEK_STARTS_ON = 5

# Query parameters that choose the period rather than narrow the rows. A
# request carrying anything beyond these (and DRF's own format switch) is a
# filtered one - see is_filtered.
PERIOD_PARAMS = {PERIOD_PARAM, DATE_FROM_PARAM, DATE_TO_PARAM, 'format'}


def period_bounds(period, today=None, date_from=None, date_to=None):
    """(start, end) for a period, both inclusive; (None, None) for 'all'.

    Raises ValueError for an unknown period or a custom one without a valid,
    ordered pair of dates.
    """
    today = today or timezone.localdate()

    if period == 'all':
        return None, None
    if period == 'today':
        return today, today
    if period == 'week':
        back = (today.weekday() - WEEK_STARTS_ON) % 7
        return today - timedelta(days=back), today
    if period == 'month':
        return today.replace(day=1), today
    if period == 'quarter':
        first_month = 3 * ((today.month - 1) // 3) + 1
        return date(today.year, first_month, 1), today
    if period == 'year':
        return date(today.year, 1, 1), today
    if period == 'custom':
        if not date_from or not date_to:
            raise ValueError('A custom period needs both date_from and date_to.')
        if date_from > date_to:
            raise ValueError('date_from must not be after date_to.')
        return date_from, date_to
    raise ValueError(f'Unknown period "{period}".')


def period_from_params(params, default='all', allowed=PERIODS, today=None):
    """(period, start, end) from query parameters, or a 400.

    ?period=custom takes ?date_from= and ?date_to= (YYYY-MM-DD). Both dates
    without a period also mean custom, so a caller can pass a range alone.
    """
    date_from = _date_param(params, DATE_FROM_PARAM)
    date_to = _date_param(params, DATE_TO_PARAM)
    period = params.get(PERIOD_PARAM) or ('custom' if date_from and date_to else default)

    if period not in allowed:
        raise ValidationError({PERIOD_PARAM: f'Choose one of: {", ".join(allowed)}.'})
    try:
        start, end = period_bounds(period, today, date_from, date_to)
    except ValueError as error:
        raise ValidationError({PERIOD_PARAM: str(error)})
    return period, start, end


def _date_param(params, name):
    raw = params.get(name)
    if not raw:
        return None
    value = parse_date(raw)
    if value is None:
        raise ValidationError({name: 'Use the format YYYY-MM-DD.'})
    return value


def period_payload(period, start, end):
    """The period as a response states it, so the page can label the figure."""
    return {
        'key': period,
        'date_from': start.isoformat() if start else None,
        'date_to': end.isoformat() if end else None,
    }


def in_period(queryset, lookup, start, end):
    """Bound a queryset to the period on `lookup` (e.g. 'date_issued__date')."""
    if start:
        queryset = queryset.filter(**{f'{lookup}__gte': start})
    if end:
        queryset = queryset.filter(**{f'{lookup}__lte': end})
    return queryset


def is_filtered(request):
    """True when the request narrows the rows - a search or any table filter."""
    return any(
        value not in (None, '')
        for key, value in request.query_params.items()
        if key not in PERIOD_PARAMS
    )


class PeriodFilterMixin:
    """
    Lets a list viewset's rows follow the page's period switch.

    Set `period_lookup` to the date the period applies to. The period narrows
    the rows after the viewset's own search and filters, so the table, and a
    `summary` action computed from filter_queryset, always describe the same
    rows. With no period parameter nothing changes: 'all' is the default.
    """

    period_lookup = None

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        if self.period_lookup and getattr(self, 'action', None) in ('list', 'summary'):
            _, start, end = period_from_params(self.request.query_params)
            queryset = in_period(queryset, self.period_lookup, start, end)
        return queryset

    def filter_queryset_without_period(self, queryset):
        """The rows the table's search and filters select, on every date."""
        return super().filter_queryset(queryset)


# ── Trends ───────────────────────────────────────────────────────────────────
# Charts take a period too. Each period is cut into buckets small enough to
# show a shape: hours for today, days for a week or a month, weeks (Saturday
# to Friday) for a quarter, months for a year.

TREND_PERIODS = ('today', 'week', 'month', 'quarter', 'year')


def trend_buckets(period, start, end):
    """[(label, first_day, last_day)] covering start..end for a period.

    'today' is handled by hour in hourly_trend, so it is not cut here.
    """
    if period in ('week', 'month'):
        days = (end - start).days + 1
        return [
            (_day_label(start + timedelta(days=i), period),
             start + timedelta(days=i), start + timedelta(days=i))
            for i in range(days)
        ]
    if period == 'quarter':
        buckets, first, number = [], start, 1
        while first <= end:
            back = (first.weekday() - WEEK_STARTS_ON) % 7
            week_end = min(first + timedelta(days=6 - back), end)
            buckets.append((f'Week {number}', first, week_end))
            first, number = week_end + timedelta(days=1), number + 1
        return buckets
    if period == 'year':
        buckets = []
        for month in range(1, end.month + 1):
            last = min(date(end.year, month, monthrange(end.year, month)[1]), end)
            buckets.append((date(end.year, month, 1).strftime('%b'), date(end.year, month, 1), last))
        return buckets
    raise ValueError(f'No buckets for "{period}".')


def _day_label(day, period):
    return day.strftime('%a') if period == 'week' else f'{day.day} {day.strftime("%b")}'


def daily_trend(rows, period, start, end):
    """[{label, date_from, date_to, value}] from (date, value) rows."""
    buckets = trend_buckets(period, start, end)
    totals = [0.0] * len(buckets)
    for day, value in rows:
        if isinstance(day, datetime):
            day = timezone.localtime(day).date() if timezone.is_aware(day) else day.date()
        for index, (_, first, last) in enumerate(buckets):
            if first <= day <= last:
                totals[index] += float(value or 0)
                break
    return [
        {'label': label, 'date_from': first.isoformat(),
         'date_to': last.isoformat(), 'value': round(total, 2)}
        for (label, first, last), total in zip(buckets, totals)
    ]


def hourly_trend(rows, today=None):
    """[{label, hour, value}] for today, from (aware datetime, value) rows.

    Runs to the current hour, so the line stops at now rather than dropping
    to zero across the hours still to come.
    """
    now = timezone.localtime()
    hours = list(range(0, now.hour + 1))
    totals = {hour: 0.0 for hour in hours}
    for moment, value in rows:
        local = timezone.localtime(moment) if timezone.is_aware(moment) else moment
        if local.hour in totals:
            totals[local.hour] += float(value or 0)
    return [
        {'label': _hour_label(hour), 'hour': hour, 'value': round(totals[hour], 2)}
        for hour in hours
    ]


def _hour_label(hour):
    suffix = 'am' if hour < 12 else 'pm'
    return f'{hour % 12 or 12} {suffix}'
