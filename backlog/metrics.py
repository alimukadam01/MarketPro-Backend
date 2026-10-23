"""
The backlog's figures (§12), worked out on read from the entries and their
reviews. Nothing is stored.

Per entry person: volume, accuracy (approved / reviewed), median seconds per
page by type, can't-read rate, and daily pages entered late (after the end of
the next working day - Sunday is not one).

Per business: pages left, pages done per day, and a projected finish at the
recent rate.
"""
import statistics
from collections import defaultdict
from datetime import timedelta

from django.db.models import Count
from django.db.models.functions import TruncDate
from django.utils import timezone

from root.utils import local_date
from .models import BacklogEntry, BacklogReview

OPEN = (BacklogEntry.PENDING, BacklogEntry.IN_PROGRESS, BacklogEntry.CANT_READ)


def next_working_day_end(day):
    """The deadline for a page uploaded on `day`: the end of the next
    working day, skipping Sunday."""
    deadline = day + timedelta(days=1)
    if deadline.weekday() == 6:
        deadline += timedelta(days=1)
    return deadline


def person_metrics(business, since=None):
    entries = BacklogEntry.objects.filter(business_id=business.id, entered_by__isnull=False)
    if since:
        entries = entries.filter(entered_at__date__gte=since)
    reviews = BacklogReview.objects.filter(entry__business_id=business.id, entered_by__isnull=False)
    if since:
        reviews = reviews.filter(created_at__date__gte=since)

    people = defaultdict(lambda: {
        'entered': 0, 'cant_read': 0, 'late_daily': 0,
        'approved': 0, 'reviewed': 0, 'seconds': defaultdict(list), 'user': None,
    })
    for entry in entries.select_related('entered_by'):
        row = people[entry.entered_by_id]
        row['user'] = entry.entered_by
        if entry.status == BacklogEntry.CANT_READ:
            row['cant_read'] += 1
            continue
        row['entered'] += 1
        if entry.seconds_spent:
            row['seconds'][entry.type].append(entry.seconds_spent)
        if entry.is_daily and entry.entered_at:
            uploaded = local_date(entry.created_at)
            if local_date(entry.entered_at) > next_working_day_end(uploaded):
                row['late_daily'] += 1

    for review in reviews.select_related('entered_by'):
        row = people[review.entered_by_id]
        row['user'] = row['user'] or review.entered_by
        row['reviewed'] += 1
        if review.outcome == BacklogReview.APPROVED:
            row['approved'] += 1

    result = []
    for user_id, row in people.items():
        user = row['user']
        flagged = row['entered'] + row['cant_read']
        all_seconds = [s for values in row['seconds'].values() for s in values]
        result.append({
            'user': {'id': user_id, 'name': (f'{user.first_name} {user.last_name}'.strip() or user.email)
                     if user else ''},
            'entered': row['entered'],
            'reviewed': row['reviewed'],
            'approved': row['approved'],
            'accuracy': round(row['approved'] / row['reviewed'] * 100, 1) if row['reviewed'] else None,
            'median_seconds': int(statistics.median(all_seconds)) if all_seconds else None,
            'median_seconds_by_type': {kind: int(statistics.median(values))
                                       for kind, values in row['seconds'].items()},
            'cant_read_rate': round(row['cant_read'] / flagged * 100, 1) if flagged else None,
            'late_daily': row['late_daily'],
        })
    return sorted(result, key=lambda item: -item['entered'])


def business_metrics(business, days=21):
    entries = BacklogEntry.objects.filter(business_id=business.id)
    today = timezone.localdate()
    start = today - timedelta(days=days - 1)

    left = entries.filter(status__in=OPEN).count()
    waiting = entries.filter(status=BacklogEntry.ENTERED).count()
    verified_month = entries.filter(status=BacklogEntry.VERIFIED,
                                    verified_at__date__gte=today.replace(day=1)).count()

    per_day = dict(
        entries.filter(entered_at__date__gte=start, entered_by__isnull=False)
        .exclude(status=BacklogEntry.CANT_READ)
        .annotate(day=TruncDate('entered_at')).values('day')
        .annotate(count=Count('id')).values_list('day', 'count'))
    done = [{'date': (start + timedelta(days=n)).isoformat(),
             'count': per_day.get(start + timedelta(days=n), 0)} for n in range(days)]

    recent = [row['count'] for row in done[-14:]]
    rate = sum(recent) / len(recent) if recent else 0
    finish = (today + timedelta(days=int(left / rate) + 1)).isoformat() if rate and left else None

    reviewed = BacklogReview.objects.filter(entry__business_id=business.id)
    approved = reviewed.filter(outcome=BacklogReview.APPROVED).count()
    total_reviewed = reviewed.count()

    return {
        'pages_left': left,
        'waiting_for_review': waiting,
        'verified_this_month': verified_month,
        'accuracy': round(approved / total_reviewed * 100, 1) if total_reviewed else None,
        'done_per_day': done,
        'average_per_day': round(rate, 1),
        'projected_finish': finish,
    }
