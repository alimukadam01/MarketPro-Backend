"""Shared pieces for the list pages' filter sets."""
from django_filters import rest_framework as filters


def number_range(field_name):
    """A (min, max) pair of filters on one numeric field, both ends included.

        total_min, total_max = number_range('total')

    An exact amount is almost never what someone filters a list by - "invoices
    over 50,000" is - and an exact match on a float is fragile anyway.
    """
    return (
        filters.NumberFilter(field_name=field_name, lookup_expr='gte'),
        filters.NumberFilter(field_name=field_name, lookup_expr='lte'),
    )


def date_range(field_name, is_datetime=False):
    """A (from, to) pair of filters on one date, both days included.

    A DateTimeField compares by calendar day in the project's time zone
    (`__date`), the day the table prints, rather than against midnight UTC.
    """
    step = 'date__' if is_datetime else ''
    return (
        filters.DateFilter(field_name=field_name, lookup_expr=f'{step}gte'),
        filters.DateFilter(field_name=field_name, lookup_expr=f'{step}lte'),
    )
