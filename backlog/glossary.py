"""
Changing the glossary. Every change makes a new version (§13): rows are never
edited, a change closes the current row and opens another, so any version
can be read back exactly (GlossaryEntry.objects.at_version).
"""
from django.db import transaction
from django.utils import timezone

from .models import Glossary, GlossaryEntry, GlossarySuggestion

LINKS = ('customer', 'supplier', 'product')
FIELDS = ('term', 'meaning', 'kind', *(f'{name}_id' for name in LINKS))


def current_version(business):
    glossary = Glossary.objects.filter(business=business).first()
    return glossary.version if glossary else 0


def _next_version(business):
    glossary, _ = Glossary.objects.select_for_update().get_or_create(business=business)
    glossary.version += 1
    glossary.save(update_fields=['version', 'updated_at'])
    return glossary.version


def add_term(business, values, user, source_entry=None):
    with transaction.atomic():
        version = _next_version(business)
        return GlossaryEntry.objects.create(
            business=business, valid_from=version, created_by=user, source_entry=source_entry,
            **{key: values.get(key) for key in FIELDS if key in values})


def change_term(row, values, user):
    """Close `row` and open its replacement; returns the new row."""
    with transaction.atomic():
        version = _next_version(row.business)
        row.valid_to = version
        row.save(update_fields=['valid_to'])
        data = {key: getattr(row, key) for key in FIELDS}
        data.update({key: values[key] for key in FIELDS if key in values})
        return GlossaryEntry.objects.create(
            business=row.business, valid_from=version, created_by=user,
            source_entry=row.source_entry, **data)


def remove_term(row, user):
    with transaction.atomic():
        version = _next_version(row.business)
        row.valid_to = version
        row.save(update_fields=['valid_to'])


def approve_suggestion(suggestion, user, overrides=None):
    """Add the suggested term (as the owner may have corrected it) and keep
    the page it came from on the new row."""
    values = {key: getattr(suggestion, key) for key in FIELDS}
    values.update(overrides or {})
    with transaction.atomic():
        row = add_term(suggestion.business, values, user, source_entry=suggestion.entry)
        suggestion.status = GlossarySuggestion.APPROVED
        suggestion.reviewed_by = user
        suggestion.reviewed_at = timezone.now()
        suggestion.glossary_entry = row
        suggestion.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'glossary_entry'])
        return row


def reject_suggestion(suggestion, user):
    suggestion.status = GlossarySuggestion.REJECTED
    suggestion.reviewed_by = user
    suggestion.reviewed_at = timezone.now()
    suggestion.save(update_fields=['status', 'reviewed_by', 'reviewed_at'])
