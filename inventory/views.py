from rest_framework.viewsets import ModelViewSet, ReadOnlyModelViewSet
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.filters import SearchFilter
from rest_framework import status
from .models import Inventory, InventoryItem
from .serializers import AvailableProductSerializer, InventoryItemCreateSerializer, InventoryItemSerializer, InventoryItemUpdateSerializer, InventorySerializer
from root.utils import get_active_business

# Create your views here.

class InventoryViewSet(ModelViewSet):

    serializer_class = InventorySerializer

    def get_queryset(self):
        business = get_active_business(self.request)
        if not business:
            return []
        
        return Inventory.objects.filter(business_id = business.id)


class InventoryItemsViewSet(ModelViewSet):

    filter_backends = [SearchFilter]
    search_fields = [
        'id', 'location__name', 'product__name', 'track_code', 'unit_cost', 'unit_price'
    ]

    def get_queryset(self):
        return InventoryItem.objects.filter(inventory_id = self.kwargs['inventory_pk'])
    
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
    def bulk_delete(self, request):
        item_ids = request.data.get('items_ids', [])
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