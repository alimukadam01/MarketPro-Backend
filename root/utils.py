import re
from datetime import datetime
from django.template.loader import get_template
from django.conf import settings
from django.utils import timezone
from django.db import transaction
from django.db.models import Q, Model
from rest_framework.viewsets import ModelViewSet
from .models import Business, Employee

def get_active_business(request):
    """The business the signed-in user is working in: the active one they
    own, else the one they are employed by.

    Looked up once per request and kept on it. A single request asks for it
    from its permission check, its queryset, its serializer context and the
    audit trail - each a query or two - so the lookups added up to most of
    some endpoints' queries (plans/performance.md, part 01). The cache is
    keyed by the user, and `forget_active_business` clears it for the one
    request that changes which business is active.
    """
    user = request.user
    if not (user and user.is_authenticated):
        return None

    holder = getattr(request, '_request', request)  # DRF wraps the HttpRequest
    cached = getattr(holder, '_active_business', None)
    if cached is not None and cached[0] == user.id:
        return cached[1]

    try:
        business = Business.objects.get(owner_id=user.id, is_active=True)
    except Business.DoesNotExist:
        business = Employee.objects.select_related('business').get(user=user).business
    holder._active_business = (user.id, business)
    return business


def forget_active_business(request):
    """Drop the cached active business, after the request switches it."""
    holder = getattr(request, '_request', request)
    if hasattr(holder, '_active_business'):
        del holder._active_business

def local_date(value=None):
    """
    The calendar date of a stored instant in the business timezone.

    Calling .date() straight on a model's datetime gives the UTC date, which
    is a day behind for the first hours of every local day. Pass nothing to
    get today.
    """
    if not value:
        return timezone.localdate()
    return timezone.localtime(value).date()

def whatsapp_number(phone, country_code='92'):
    """
    A phone number in the form wa.me expects: digits only, country code
    included.

    Numbers are typed the way they are dialled locally ("0331-3689402"), and a
    wa.me link built from that opens a chat with the wrong number or none at
    all. The leading zero has to become the country code.
    """
    digits = re.sub(r'\D', '', phone or '')
    if not digits:
        return ''

    if digits.startswith('00'):
        digits = digits[2:]
    if digits.startswith(country_code):
        return digits

    return country_code + digits.lstrip('0')

def generateTransactionId(instance: Model):
    
    """
    Generates a transaction ID in the format:
    <MODEL_INITIALS>-<YYYYMMDDHHMMSS>-<PRODUCT_ID>-<QUANTITY (optional)>
    """
    
    model_name = instance.__class__.__name__
    initials = ''.join([char for char in model_name if char.isupper()]) or model_name[:3].upper()

    timestamp = timezone.localtime().strftime("%Y%m%d%H%M%S")

    try:
        product_id = instance.product.id
    except AttributeError:
        product_id = "UNKNOWN"

    # Try extracting `quantity`
    quantity = getattr(instance, "quantity", None)

    parts = [initials, str(instance.id), timestamp, str(product_id)]
    if quantity is not None:
        parts.append(str(quantity))

    return "-".join(parts)

def build_search_q(fields, query):
    """
    Build an OR Q() object for all provided fields using icontains.
    `fields` is an iterable of field names (strings).
    """
    q = Q()
    for field in fields:
        q |= Q(**{f"{field}__icontains": query})
    return q

def serialize_queryset(qs, serializer_class, result_type, many=True, limit=None):
    """
    Serialize queryset (optionally slice it), and attach a `type` field to each item.
    Returns a list of dicts.
    """
    if limit:
        qs = qs[:limit]
    serialized = serializer_class(qs, many=many).data
    # attach type to each result
    for item in serialized:
        item["type"] = result_type
    return serialized

def generate_sku(business_id, prefix="MP"):
    """
    Generates a business-level unique SKU.
    Example: MP-000001
    """

    with transaction.atomic():
        # Lock the business row
        business = Business.objects.get(id=business_id)

        sequence = business.sku_counter
        sku = f"{prefix}-{sequence:06d}"

        business.sku_counter += 1
        business.save(update_fields=["sku_counter"])

    return sku

def generate_variant_name(attributes = None):

    if not attributes:
        return 'default'
    
    values = [
        str(attributes[key]).strip()
        for key in attributes.keys()
        if attributes[key] not in (None, "", [])
    ]


    return " / ".join(values)
