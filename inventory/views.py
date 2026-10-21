from rest_framework.viewsets import ModelViewSet, GenericViewSet, ReadOnlyModelViewSet
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.filters import SearchFilter
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import status
from root.utils import get_active_business
from root.models import BaseQuerySet
from .filters import InventoryItemFilter
from .models import Inventory, InventoryItem
from .serializers import AvailableProductSerializer, InventoryItemCreateSerializer, InventoryItemSerializer, InventoryItemUpdateSerializer, InventorySerializer

# Create your views here.

class InventoryViewSet(ModelViewSet):

    serializer_class = InventorySerializer

    def get_queryset(self):
        business = get_active_business(self.request)
        if not business:
            return []
        
        return Inventory.objects.filter(business_id = business.id)


class InventoryItemsViewSet(ModelViewSet):

    filter_backends = [SearchFilter, DjangoFilterBackend]
    filterset_class = InventoryItemFilter
    search_fields = [
        'id', 'location__name', 'product__base__name', 'product__name', 'track_code', 'unit_cost', 'unit_price'
    ]

    def get_queryset(self):
        """
        One business's stock, A-Z.

        Ordered by the base product then the variant, which is how both
        InventoryItem.__str__ and transformInventoryItem on the client build
        the "Product" cell - so the order matches the text on screen. Without
        an order_by the database returned rows in whatever order it liked.

        Scoped by business rather than by the inventory_pk in the URL. An
        Inventory is one-per-business, but the two primary keys are separate
        sequences that only coincide because the signal has created them in
        step so far; InventoryQuerySet.get_items was already fixed for this
        and says so, and this read was the one left trusting the id.

        select_related because the nested ProductVariantSerializer reaches
        product.base on every row - the ordering forces those joins anyway.
        """
        business = get_active_business(self.request)
        if not business:
            return InventoryItem.objects.none()

        return (
            InventoryItem.objects
            .filter(inventory__business_id=business.id)
            .select_related('product__base', 'location')
            .order_by('product__base__name', 'product__name')
        )
    
    def get_serializer_class(self):
        method = self.request.method
        
        if method == 'POST':
            return InventoryItemCreateSerializer
        
        if method in ('PUT', 'PATCH'):
            return InventoryItemUpdateSerializer
        
        return InventoryItemSerializer

    def get_serializer_context(self):
        
        business = get_active_business(self.request)
        if not business:
            return {}
        
        return {
            'business_id': business.id,
            'inventory_id': self.kwargs['inventory_pk']
        }
    
    @action(['GET'], False)
    def get_available_items(self, pk=None, inventory_pk=None):
        try:
            available_items = InventoryItem.objects.filter(
                inventory_id=self.kwargs['inventory_pk'],
                quantity_on_hand__gt=0
            )

            available_products = [{
                "product": item.product, 
                "available_quantity": item.quantity_on_hand,
                "unit_price": item.unit_price
            } for item in available_items]

            serializer = AvailableProductSerializer(available_products, many=True)
            
            return Response(
                data=serializer.data, 
                status=status.HTTP_200_OK
            )

        except Exception as error:
            print(error)
            return Response({
                "detail": "Internal Server Error"
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
    @action(['POST'], detail=False, url_path='bulk-delete', url_name='bulk-delete')
    def bulk_delete(self, request, inventory_pk=None):
        item_ids = request.data.get('item_ids', [])
        print(item_ids)
        if not item_ids:
            return Response({
                'detail': 'Bad Request.'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            InventoryItem.objects.filter(id__in=item_ids).delete()
            return Response({
                'detail': 'Success.'
            }, status=status.HTTP_200_OK)
        
        except Exception as error:
            print(error)
            return Response({
                'detail': 'Internal Server Error.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class InventoryKPIViewSet(GenericViewSet):
    serializer_class = None

    @action(['GET'], detail=False, url_name='total-inventory-value', url_path='total-inventory-value')
    def total_inventory_value(self, request):
        if request.method == 'GET':
            business = get_active_business(request)
            if not business:
                return Response({
                    'detail': 'No active business exists. Please contact admin.'
                }, status=status.HTTP_400_BAD_REQUEST)

            total_inventory_value = Inventory.objects.total_inventory_value(business.id)
            return Response({
                "total_inventory_value": total_inventory_value
            }, status=status.HTTP_200_OK)
        
        return Response({
            "detail": "Method not allowed"
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)
    
    @action(['GET'], detail=False, url_name='total-inventory-value-with-profit', url_path='total-inventory-value-with-profit')
    def total_inventory_value_with_profit(self, request):
        business = get_active_business(request)
        if not business:
            return Response({
                'detail': 'No active business exists. Please contact admin.'
            }, status=status.HTTP_400_BAD_REQUEST)

        total = Inventory.objects.total_inventory_value_with_profit(business.id)
        return Response({
            "total_inventory_value_with_profit": total
        }, status=status.HTTP_200_OK)

    @action(['GET'], detail=False, url_name='total-items-not-in-inventory', url_path='total-items-not-in-inventory')
    def total_items_not_in_inventory(self, request):
        business = get_active_business(request)
        if not business:
            return Response({
                'detail': 'No active business exists. Please contact admin.'
            }, status=status.HTTP_400_BAD_REQUEST)

        count = Inventory.objects.total_items_not_in_inventory(business.id)
        return Response({
            "items_not_in_inventory": count
        }, status=status.HTTP_200_OK)

    @action(['GET'], detail=False, url_name='total-restocks-required', url_path='total-restocks-required')
    def total_restocks_required(self, request):
        if request.method == 'GET':
            business = get_active_business(request)
            
            if not business:
                return Response({
                    'detail': 'No active business exists. Please contact admin.'
                }, status=status.HTTP_400_BAD_REQUEST)

            restocks_req = Inventory.objects.total_restocks_required(business.id)
            return Response({
                "restocks_req": restocks_req
            }, status=status.HTTP_200_OK)
        
        return Response({
            "detail": "Method not allowed"
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)
    
 