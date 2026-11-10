"""What an invoice line carries beyond product, quantity and price.

Two things, both on sales and purchase lines (plans/product-extra-fields.md):

- extra_fields: the answers to the product's extra fields, {"<field id>": value}
  - Urban Paints' colour code, {"3": "5674"}.
- discount: off the line, {"type": "percentage" | "amount", "value": n}.
  Never empty or NULL: no discount is 0%, {"type": "percentage", "value": 0}.

Both sit in the lines' UNIQUE keys, so two gallons in different colours, or at
different discounts, are two lines. That only works if equal values are equal
byte for byte, so the server writes one canonical form and nothing else does:
keys sorted, blanks dropped, numbers normalised. No answers are {}; no discount
is NO_DISCOUNT. Neither is ever NULL.

Nothing here touches stock. A line finds its stock row by (business, product),
and neither of these takes part.
"""
import json
import math

from rest_framework import serializers

SALE = 'sale'
PURCHASE = 'purchase'

MAX_TEXT = 100

PERCENTAGE = 'percentage'
AMOUNT = 'amount'


def no_discount():
    """A line's discount when it has none: 0%. A function, so each line gets
    its own dict (the model field's default)."""
    return {'type': PERCENTAGE, 'value': 0}


NO_DISCOUNT = no_discount()


def _number(value):
    """A finite number, as an int when it is whole (so 5674.0 and 5674 are the
    same answer), else rounded to 2 dp. None when it is not a number."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    number = round(number, 2)
    return int(number) if number.is_integer() else number


# ── extra fields ─────────────────────────────────────────────────────────────

def asked_links(variant, side):
    """The product's active links that ask on this side, by field id."""
    if variant is None:
        return {}
    # .all(), so a prefetch of base__field_links__field is used.
    links = variant.base.field_links.all()
    return {
        link.field_id: link for link in links
        if link.field.is_active and link.asks_on(side)
    }


def clean_extra_fields(variant, answers, side, label=None):
    """The canonical extra_fields for a new line of `variant`, or a 400.

    answers: {"<field id>": value} as sent; missing or None means none.
    side: 'sale' or 'purchase' - a field asked on sales only is not a
    purchase line's to answer.
    """
    if answers in (None, ''):
        answers = {}
    if not isinstance(answers, dict):
        raise serializers.ValidationError('Extra fields must be an object of field id to answer.')

    links = asked_links(variant, side)
    what = label or (str(variant) if variant else 'this product')
    cleaned = {}

    for raw_id, value in answers.items():
        try:
            field_id = int(raw_id)
        except (TypeError, ValueError):
            raise serializers.ValidationError(f'"{raw_id}" is not an extra field.')
        link = links.get(field_id)
        if link is None:
            raise serializers.ValidationError(f'That extra field is not asked for {what}.')
        field = link.field

        if value is None or (isinstance(value, str) and not value.strip()):
            continue  # blank: dropped, and caught below if it was required

        if field.field_type == field.NUMBER:
            number = _number(value)
            if number is None:
                raise serializers.ValidationError(f'{field.label} must be a number.')
            cleaned[str(field_id)] = number
        else:
            text = str(value).strip()
            if len(text) > MAX_TEXT:
                raise serializers.ValidationError(
                    f'{field.label} can be at most {MAX_TEXT} characters.')
            cleaned[str(field_id)] = text

    for field_id, link in links.items():
        if link.required and str(field_id) not in cleaned:
            raise serializers.ValidationError(
                f'Enter the {link.field.label.lower()} for {what}.')

    return {key: cleaned[key] for key in sorted(cleaned, key=int)}


def field_labels(business_id):
    """{field id: label} for a business, every field - retired ones too, since
    old lines still show their answers."""
    from root.models import ProductField
    return dict(ProductField.objects.filter(business_id=business_id).values_list('id', 'label'))


def display_extra_fields(extra_fields, labels):
    """[{label, value}] in field order, for the screens and the PDF. The label
    is the field's current name: a rename shows on every invoice."""
    rows = []
    for key in sorted((extra_fields or {}), key=lambda k: int(k) if str(k).isdigit() else 0):
        label = labels.get(int(key)) if str(key).isdigit() else None
        rows.append({'label': label or 'Detail', 'value': extra_fields[key]})
    return rows


# ── discount ─────────────────────────────────────────────────────────────────

def canonical_discount(raw):
    """The stored form of a discount, without checking it against a line:
    0% for none, zero or anything unreadable - one form, so the UNIQUE key
    sees every undiscounted line alike."""
    if not raw or not isinstance(raw, dict):
        return no_discount()
    kind = raw.get('type')
    value = _number(raw.get('value'))
    if kind not in (PERCENTAGE, AMOUNT) or value is None or value <= 0:
        return no_discount()
    return {'type': kind, 'value': value}


def clean_line_discount(raw, gross):
    """The canonical discount for a new line whose full amount is `gross`, or
    a 400. A PKR discount is off the whole line. Missing or blank is 0%: the
    stored discount is never empty."""
    if raw in (None, '', {}):
        return no_discount()
    if not isinstance(raw, dict):
        raise serializers.ValidationError('A discount is {"type": "percentage" or "amount", "value": n}.')
    kind = raw.get('type')
    if kind not in (PERCENTAGE, AMOUNT):
        raise serializers.ValidationError('A discount is a percentage or an amount.')
    value = _number(raw.get('value'))
    if value is None:
        raise serializers.ValidationError('The discount must be a number.')
    if value < 0:
        raise serializers.ValidationError('The discount cannot be negative.')
    if value == 0:
        return no_discount()
    if kind == PERCENTAGE and value > 100:
        raise serializers.ValidationError('A discount cannot be more than 100%.')
    if kind == AMOUNT and value > round(gross, 2):
        raise serializers.ValidationError('The discount is more than the line.')
    return {'type': kind, 'value': value}


def discount_off(discount, gross, quantity, net_quantity):
    """What a line's discount takes off. A percentage of the line; an amount
    is off the whole line, and shrinks with a return (half returned, half the
    discount)."""
    if not discount:
        return 0.0
    value = float(discount.get('value') or 0)
    if discount.get('type') == PERCENTAGE:
        return gross * value / 100
    if discount.get('type') == AMOUNT:
        if not quantity:
            return 0.0
        return value * net_quantity / quantity
    return 0.0


def line_total(price, quantity, net_quantity, discount):
    """A line after its discount: the one sum the totals, the screens and the
    PDF all use."""
    gross = (net_quantity or 0) * (price or 0)
    return gross - discount_off(discount, gross, quantity, net_quantity)


# ── one line as sent ─────────────────────────────────────────────────────────

def clean_new_line(item, variant, side, price_key):
    """extra_fields and discount of one line from an invoice payload, in place.
    For a line being created: an existing line keeps what it has."""
    quantity = _number(item.get('quantity')) or 0
    gross = quantity * (_number(item.get(price_key)) or 0)
    item['extra_fields'] = clean_extra_fields(variant, item.get('extra_fields'), side)
    item['discount'] = clean_line_discount(item.get('discount'), gross)
    return item


def line_key(product_id, price, extra_fields, discount):
    """What makes two lines the same line: the UNIQUE key, in Python."""
    return (
        str(product_id),
        _number(price),
        json.dumps(extra_fields or {}, sort_keys=True),
        json.dumps(discount or {}, sort_keys=True),
    )


def _id(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def clean_payload_lines(items, business_id, side, price_key, saved=None):
    """Check and canonicalise every line of a nested invoice payload, in place.

    New lines get their extra_fields and discount cleaned against their
    product. A line already saved (its id in `saved`) keeps what it has: a
    line is not edited in place, so anything sent for it is replaced by the
    stored values.

    Then no two lines may be the same line - product, price, answers and
    discount - which would otherwise be a 500 from the UNIQUE key. The screens
    merge such lines; this is the backstop for anything else that posts.
    """
    from root.models import ProductVariant

    if not isinstance(items, list):
        raise serializers.ValidationError('Items must be a list.')
    saved = saved or {}
    wanted = {_id(item.get('product_id')) for item in items if isinstance(item, dict)}
    variants = {
        variant.id: variant for variant in
        ProductVariant.objects.filter(base__business_id=business_id, id__in=wanted - {None})
        .select_related('base').prefetch_related('base__field_links__field')
    }

    seen = {}
    for number, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise serializers.ValidationError(f'Line {number} is not a line.')
        line = saved.get(_id(item.get('id')))
        try:
            if line is not None:
                item['extra_fields'] = line.extra_fields or {}
                item['discount'] = line.discount or {}
            else:
                variant = variants.get(_id(item.get('product_id')))
                if variant is None:
                    raise serializers.ValidationError('Pick a product from this business.')
                clean_new_line(item, variant, side, price_key)
        except serializers.ValidationError as error:
            detail = error.detail[0] if isinstance(error.detail, list) else error.detail
            raise serializers.ValidationError(f'Line {number}: {detail}')

        key = line_key(item.get('product_id'), item.get(price_key),
                       item['extra_fields'], item['discount'])
        if key in seen:
            raise serializers.ValidationError(
                f'Lines {seen[key]} and {number} are the same product at the same price, with the same '
                f'details and discount. Change the quantity on line {seen[key]} instead.')
        seen[key] = number
    return items
