"""
Running a query over every search type the user may see.

Each visible type costs one count; a type with matches costs one more query
for its five hits (plus what it prefetches) and, for a breakdown, one more.
Types the user cannot see are never queried. Empty groups are left out, so
an answer with no groups means nothing matched.
"""
from django.db.models import Count

from . import matching
from .access import Access
from .registry import registered

MAX_HITS = 5


def _breakdown(search_type, queryset):
    labels = dict(search_type.choices(search_type.breakdown))
    rows = (queryset.order_by()
            .values(search_type.breakdown)
            .annotate(count=Count("pk", distinct=True))
            .order_by("-count"))
    return [
        {"label": str(labels.get(row[search_type.breakdown], row[search_type.breakdown])),
         "count": row["count"]}
        for row in rows
    ]


def search(request, business, raw_query):
    query = (raw_query or "").strip()
    if not matching.is_searchable(query):
        return {"query": query, "groups": []}

    access = Access(request, business)
    groups = []
    for search_type in registered():
        if not access.can_view(search_type):
            continue
        condition, annotations = matching.build(search_type, query)
        if condition is None:
            continue

        queryset = search_type.base_queryset(request, business, access)
        if annotations:
            queryset = queryset.annotate(**annotations)
        queryset = queryset.filter(condition)
        if search_type.distinct:
            queryset = queryset.distinct()

        total = queryset.count()
        if not total:
            continue

        rows = queryset
        if search_type.select_related:
            rows = rows.select_related(*search_type.select_related)
        if search_type.prefetch_related:
            rows = rows.prefetch_related(*search_type.prefetch_related)
        rows = list(rows.order_by(*search_type.order_by)[:MAX_HITS])
        if search_type.prepare is not None:
            search_type.prepare(rows)

        group = {
            "type": search_type.key,
            "label": search_type.label,
            "total": total,
            "hits": [search_type.to_hit(row).as_dict() for row in rows],
        }
        if search_type.breakdown:
            group["breakdown"] = _breakdown(search_type, queryset)
        groups.append(group)

    return {"query": query, "groups": groups}
