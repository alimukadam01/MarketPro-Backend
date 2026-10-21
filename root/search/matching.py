"""
Turning a query into a filter for one search type.

  text     - icontains
  number   - exact, and only when the query is a number: "612" finds
             invoice 612 and PKR 612, never PKR 1,612
  phone    - digits only on both sides, so 0300-1234567, 0300 1234567,
             03001234567 and +923001234567 all find one another
  choice   - the query is matched against each choice's LABEL and the
             matching codes are searched. A code is never matched as text:
             that is what made "c" match every completed invoice.

A query of several words matches a record when every word matches one of
its fields, not necessarily the same one: "Royal Fan White" finds the White
variant of the Royal Fan, whose name and variant name are separate fields.
The list pages' search boxes (DRF's SearchFilter) work the same way. The
whole phrase still matches too, which is what finds a phone number typed
with spaces in it.
"""
import re
from decimal import Decimal, InvalidOperation

from django.db import models
from django.db.models import F, Q, Value
from django.db.models.functions import Replace

MIN_LENGTH = 2
NUMBER = re.compile(r"^\d[\d,]*(\.\d+)?$")

# Characters people put in phone numbers, removed before comparing.
PHONE_NOISE = ("-", " ", "+", "(", ")", ".")


def is_searchable(query):
    """Two characters or more, or any number."""
    return len(query) >= MIN_LENGTH or bool(NUMBER.match(query))


def as_number(query):
    if not NUMBER.match(query):
        return None
    try:
        return Decimal(query.replace(",", ""))
    except InvalidOperation:
        return None


def phone_digits(query):
    """The digits to look for in a phone number, or None.

    The last ten digits, when there are that many, so a number typed with
    0, 92 or +92 in front finds the same phone however it was stored.
    """
    digits = re.sub(r"\D", "", query)
    if len(digits) < 4:
        return None
    return digits[-10:] if len(digits) >= 10 else digits


def _resolve(model, path):
    """The model field at the end of a lookup path."""
    field = None
    for part in path.split("__"):
        field = model._meta.get_field(part)
        if field.is_relation and field.related_model is not None:
            model = field.related_model
    return field


def _digits_only(path):
    expression = F(path)
    for character in PHONE_NOISE:
        expression = Replace(expression, Value(character), Value(""))
    return expression


def build(search_type, query):
    """(Q, annotations) matching `query` for this type, or (None, {}) when
    nothing about the query can match it."""
    annotations = {}
    phrase = _match(search_type, query, annotations)

    # Words with no letter or digit in them ("/", "-") would match almost
    # anything, so they say nothing about what is wanted.
    words = [word for word in query.split() if re.search(r"\w", word)]
    if len(words) < 2:
        return (phrase, annotations) if phrase is not None else (None, {})

    every_word = Q()
    for word in words:
        part = _match(search_type, word, annotations)
        if part is None:
            # A word nothing on this type can match: no record has them all.
            every_word = None
            break
        every_word &= part

    condition = phrase
    if every_word is not None:
        condition = every_word if condition is None else condition | every_word
    return (condition, annotations) if condition is not None else (None, {})


def _match(search_type, query, annotations):
    """The Q matching `query` as one term on any of this type's fields, or
    None when none of them can. Phone annotations are added to
    `annotations`; they depend only on the field, so words share them."""
    condition = Q()
    matched_any = False

    for path in search_type.text_fields:
        condition |= Q(**{f"{path}__icontains": query})
        matched_any = True

    number = as_number(query)
    if number is not None:
        for path in search_type.number_fields:
            field = _resolve(search_type.model, path)
            if isinstance(field, (models.IntegerField, models.AutoField, models.ForeignKey)):
                if number != number.to_integral_value():
                    continue
                value = int(number)
            else:
                value = float(number)
            condition |= Q(**{path: value})
            matched_any = True

    digits = phone_digits(query)
    if digits:
        for index, path in enumerate(search_type.phone_fields):
            name = f"_search_phone_{index}"
            annotations[name] = _digits_only(path)
            condition |= Q(**{f"{name}__contains": digits})
            matched_any = True

    needle = query.lower()
    for path in search_type.choice_fields:
        codes = [code for code, label in search_type.choices(path)
                 if needle in str(label).lower()]
        if codes:
            condition |= Q(**{f"{path}__in": codes})
            matched_any = True

    return condition if matched_any else None
