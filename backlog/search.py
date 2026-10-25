"""What the backlog app makes searchable: backlog entries (pages to enter).

Seen by whoever can view the backlog module - the owner and employees given
it. Data entry accounts never reach global search (audit/authentication.py).
"""
from root.search import Hit, SearchType, link, register, status
from root.search.labels import joined

from .models import BacklogEntry

_TONES = {
    BacklogEntry.PENDING: 'neutral',
    BacklogEntry.IN_PROGRESS: 'neutral',
    BacklogEntry.ENTERED: 'warning',
    BacklogEntry.VERIFIED: 'success',
    BacklogEntry.CANT_READ: 'danger',
}


def _first_line(text):
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line if len(line) <= 60 else line[:57] + "..."


register(SearchType(
    key="backlog_entry",
    label="Backlog entries",
    model=BacklogEntry,
    module="backlog_entries",
    text_fields=["book_label", "notes", "review_note", "cant_read_note"],
    number_fields=["id"],
    choice_fields={"type": None, "status": None},
    order_by=["-created_at", "-id"],
    priority=250,
    to_hit=lambda entry: Hit(
        id=entry.id,
        title=f"{entry.get_type_display()} #{entry.id}",
        subtitle=joined(entry.book_label, _first_line(entry.review_note or entry.notes)),
        date=entry.created_at,
        status=status(entry.status, entry.get_status_display(), _TONES.get(entry.status, 'neutral')),
        link=link("/backlog", entry_id=entry.id),
    ),
))
