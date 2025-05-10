from rest_framework import serializers
from root.models import Location
from root.serializers import BaseItemSerializer
from .models import Inventory, InventoryItem

class InventoryItemSerializer(BaseItemSerializer):

    location = serializers.PrimaryKeyRelatedField(queryset=Location.objects.none())
    quantity_on_hand = serializers.IntegerField()
    quantity_reserved = serializers.IntegerField()
    unit_cost = serializers.FloatField()
    reorder_level = serializers.IntegerField()
    last_transaction_id = serializers.IntegerField(read_only=True)
    last_transaction_at = serializers.DateTimeField(read_only=True)
    restock_needed = serializers.SerializerMethodField()

    def get_restock_needed(self, obj: InventoryItem):

        if not obj.reorder_level:
            return None 

        if obj.quantity_on_hand > obj.reorder_level:
            return False
        
        return True


class InventoryItemCreateSerializer(serializers.ModelSerializer):

    def save(self, **kwargs):
        return InventoryItem.objects.create(
            business_id = self.context['business_id'], 
            inventory_id = self.context['inventory_id'],
            **self.validated_data
        )

    class Meta:
        model = InventoryItem
        fields = [
            'id', 'product', 'quantity', 'track_code', 
            'notes', 'location', 'quantity_on_hand', 'quantity_reserved',
            'unit_cost', 'reorder_level' 
        ]


class InventoryItemUpdateSerializer(serializers.ModelSerializer):

    def save(self, **kwargs):
        
        for attr, value in self.validated_data.items():
            setattr(self.instance, attr, value)

        self.instance.save()
        return self.instance

    class Meta:
        model = InventoryItem
        fields = [
            'id', 'location', 'quantity', 'track_code', 
            'notes', 'unit_cost', 'reorder_level'
        ]


class InventorySerializer(serializers.ModelSerializer):

    class Meta:
        model = Inventory
        fields = [
            'id', 'business', 'total_quantity_on_hand', 'total_value_reserved', 
            'net_inventory_value', 'last_transaction', 'last_transaction_at'
        ]

