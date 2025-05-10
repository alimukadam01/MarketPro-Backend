from rest_framework.viewsets import ModelViewSet, ReadOnlyModelViewSet
from .models import Inventory, InventoryItem
from .serializers import InventoryItemCreateSerializer, InventoryItemSerializer, InventoryItemUpdateSerializer, InventorySerializer
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
    
