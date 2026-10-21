"""What the backlog app makes searchable: backlog entries.

Visibility is the backlog's own rule, the one its viewset applies: an admin
sees every entry, an employee only the open entries assigned to them.
"""
from root.search import Hit, SearchType, link, register, status
from root.search.labels import joined, person_name

from .models import BacklogEntry


def _visible_entries(request, business, access):
    if access.is_admin:
        return BacklogEntry.objects.filter(business_id=business.id)
    return BacklogEntry.objects.for_employee(business.id, request.user.id)


def _first_line(text):
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line if len(line) <= 60 else line[:57] + "..."


register(SearchType(
    key="backlog_entry",
    label="Backlog entries",
    model=BacklogEntry,
    queryset=_visible_entries,
    text_fields=["notes", "assigned_to__user__first_name", "assigned_to__user__last_name",
                 "assigned_to__user__email"],
    number_fields=["id"],
    choice_fields={"type": None},
    select_related=["assigned_to__user"],
    order_by=["-created_at", "-id"],
    priority=250,
    to_hit=lambda entry: Hit(
        id=entry.id,
        title=entry.get_type_display(),
        subtitle=joined(person_name(getattr(entry.assigned_to, "user", None)),
                        _first_line(entry.notes)),
        date=entry.created_at,
        status=status("DONE", "Done", "success") if entry.is_done else None,
        link=link("/backlog/update-entry", entry_id=entry.id),
    ),
))
