from django.db.models import Q

from inventory.models import InventoryItem
from inventory.serializers import InventoryItemSerializer
from sales.models import PurchaseInvoice, PurchaseInvoiceItem, ReturnedItem, SalesInvoice, SalesInvoiceItem
from sales.serializers import PurchaseInvoiceItemSerializer, ReturnedItemSerializer, SalesInvoiceItemSerializer, SalesInvoiceSerializer, SimplePurchaseInvoiceSerializer
from .models import Customer, Product, Location, Supplier
from .serializers import CustomerSerializer, ProductSerializer, LocationSerializer, SimpleCustomerSerializer, SupplierSerializer

class MultiModelSearchEngine:
    """
    A flexible search engine that can run search operations across
    multiple Django models, serialize the results, and format a
    unified response.

    Members:
        queryset     - The base queryset (optional; depends on usage)
        models       - A dict {ModelClass: [field1, field2, ...]}
        serializers  - A dict {ModelClass: SerializerClass}
    """

    def __init__(self, queryset=None):
        self.queryset = queryset
        self.models = {}         # {Model: [fields]}
        self.serializers = {}    # {Model: Serializer}

    # -----------------------------
    # Model + Serializer Management
    # -----------------------------

    def set_models(self, models: dict):
        """
        Replace all models with the provided {Model: [fields]} mapping.
        """
        self.models = models

    def add_model(self, model, fields: list):
        """
        Add a model + fields to the search configuration.
        """
        self.models[model] = fields

    def set_serializers(self, serializers: dict):
        """
        Replace all serializers with {Model: SerializerClass}.
        """
        self.serializers = serializers

    def add_serializer(self, model, serializer):
        """
        Add a serializer for a given model.
        """
        self.serializers[model] = serializer

    # -----------------------------
    # Response Formatting
    # -----------------------------

    def format_response(self, results):
        """
        Format search results into the final response structure.
        """
        raise NotImplementedError

    # -----------------------------
    # Core Search Logic
    # -----------------------------

    def search(self, key: str):
        """
        Execute a search across all registered models using the
        configured fields, returning raw serialized data.
        """
        if not key or not isinstance(key, str):
            return []

        results = []

        for model, fields in self.models.items():
            if not fields:
                continue

            try:
                queryset = model.objects.all()
            except Exception:
                continue  # avoids model misconfigurations

            # Build OR-based Q object across all fields
            q_obj = Q()
            for field in fields:
                try:
                    lookup = f"{field}__icontains"
                    q_obj |= Q(**{lookup: key})

                except Exception:
                    # Skip invalid lookups (numeric fields etc.)
                    continue

            try:
                filtered = queryset.filter(q_obj)
            except Exception:
                continue

            serializer_class = self.serializers.get(model)
            if serializer_class:
                try:
                    serialized = serializer_class(filtered, many=True).data

                except Exception:
                    raise Exception("Error serializing objects")

            else:
                raise Exception(f"No serializer found for model: {model.__name__}")

            results.append({
                "model": f"{model.__name__}",
                "count": len(serialized),
                "results": serialized,
            })

        return results


GlobalSearch = MultiModelSearchEngine()
GlobalSearch.set_models({
    SalesInvoice: [
        'id', 'invoice_number', 'customer__name', 'status', 'payment_status', 'sub_total', 'total', 'discount', 'tax', 'notes', 'created_by__email'
    ],
    SalesInvoiceItem: [
        'id', 'sales_invoice__id', 'sales_invoice__invoice_number', 'product__name', 'track_code', 'quantity_received', 'unit_price', 'discount'
    ],
    PurchaseInvoice: [
        'id', 'invoice_number', 'supplier__name', 'status', 'payment_status',
        'sub_total', 'total', 'amount_paid', 'goods_received', 'delivery', 'notes'
    ],
    PurchaseInvoiceItem: [
        'id', 'purchase_invoice__id', 'purchase_invoice__invoice_number', 'product__name',
        'track_code', 'notes', 'unit_cost', 'quantity_received'
    ],
    InventoryItem: [
        'id', 'location__name', 'product__name', 'track_code', 'unit_cost', 'unit_price'
    ],
    ReturnedItem: [
        'id', 'invoice_item__sales_invoice__id', 'invoice_item__product__name'
    ],
    Product: [
        'id', 'name', 'desc', 'unit__name'
    ],
    Customer: [
        'id', 'name', 'phone', 'email', 'address', 'city__name'
    ],
    Supplier: [
        'id', 'name', 'business_name', 'phone', 'email', 'notes'
    ],
    Location: [
        'id', 'name', 'address'
    ]
})
GlobalSearch.set_serializers({
    SalesInvoice: SalesInvoiceSerializer,
    SalesInvoiceItem: SalesInvoiceItemSerializer,
    PurchaseInvoice: SimplePurchaseInvoiceSerializer,
    PurchaseInvoiceItem: PurchaseInvoiceItemSerializer,
    InventoryItem: InventoryItemSerializer,
    ReturnedItem: ReturnedItemSerializer,
    Product: ProductSerializer,
    Customer: SimpleCustomerSerializer,
    Supplier: SupplierSerializer,
    Location: LocationSerializer
})
