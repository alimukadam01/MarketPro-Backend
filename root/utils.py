from django.db.models import Q, Model
from .models import Business
from datetime import datetime
from django.apps import apps

def get_active_business(request):

    if request.user and request.user.is_authenticated:
        try:
            return Business.objects.get(owner_id = request.user.id, is_active=True)
        except Exception as error:
            print(error)
            return None
    return None

def generateTransactionId(instance: Model):
    
    """
    Generates a transaction ID in the format:
    <MODEL_INITIALS>-<YYYYMMDDHHMMSS>-<PRODUCT_ID>-<QUANTITY (optional)>
    """
    
    model_name = instance.__class__.__name__
    initials = ''.join([char for char in model_name if char.isupper()]) or model_name[:3].upper()

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

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
