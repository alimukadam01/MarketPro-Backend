from django.utils import timezone
from rest_framework import serializers
from root.models import Location
from root.serializers import BaseItemSerializer, ProductVariantSerializer, SimpleProductVariantSerializer
from .models import Inventory, InventoryItem

class InventoryItemSerializer(BaseItemSerializer):

    product = ProductVariantSerializer()
    location = serializers.PrimaryKeyRelatedField(queryset=Location.objects.none())
    quantity = serializers.IntegerField()
    quantity_on_hand = serializers.IntegerField()
    quantity_reserved = serializers.IntegerField()
    unit_cost = serializers.FloatField()
    unit_price = serializers.FloatField()
    reorder_level = serializers.IntegerField()
    last_transaction = serializers.CharField(read_only=True)
    stock_counted_at = serializers.DateField(read_only=True)
    restock_needed = serializers.SerializerMethodField()

    def get_restock_needed(self, obj: InventoryItem):

        if not obj.reorder_level:
            return None 

        if obj.quantity_on_hand > obj.reorder_level:
            return False
        
        return True


def stamp_count(values, quantity_set):
    """A person typing a quantity has counted the stock (§8): date the count
    today, unless the date was given (an import says when its file was true)."""
    if quantity_set and not values.get('stock_counted_at'):
        values['stock_counted_at'] = timezone.localdate()
    return values


class InventoryItemCreateSerializer(serializers.ModelSerializer):

    def save(self, **kwargs):
        values = stamp_count(dict(self.validated_data),
                             'quantity' in self.validated_data or 'quantity_on_hand' in self.validated_data)
        return InventoryItem.objects.create(
            business_id = self.context['business_id'], 
            inventory_id = self.context['inventory_id'],
            **values
        )

    class Meta:
        model = InventoryItem
        fields = [
            'id', 'product', 'quantity', 'track_code', 
            'notes', 'location', 'quantity_on_hand', 'quantity_reserved',
            'unit_cost', 'unit_price', 'reorder_level', 'stock_counted_at',
        ]


class InventoryItemUpdateSerializer(serializers.ModelSerializer):

    def save(self, **kwargs):
        # Only a changed quantity is a count; a price edit is not (§8).
        counted = any(
            field in self.validated_data and self.validated_data[field] != getattr(self.instance, field)
            for field in ('quantity', 'quantity_on_hand'))
        values = stamp_count(dict(self.validated_data), counted)

        for attr, value in values.items():
            setattr(self.instance, attr, value)

        self.instance.save()
        return self.instance

    class Meta:
        model = InventoryItem
        fields = [
            'id', 'product', 'location', 'quantity', 'quantity_on_hand', 'quantity_reserved','track_code', 
            'notes', 'unit_cost', 'unit_price', 'reorder_level', 'stock_counted_at',
        ]


class InventorySerializer(serializers.ModelSerializer):

    class Meta:
        model = Inventory
        fields = [
            'id', 'business', 'total_quantity_on_hand', 'total_value_reserved', 
            'net_inventory_value', 'last_transaction'
        ]


class AvailableProductSerializer(serializers.Serializer):

    product = ProductVariantSerializer()
    available_quantity = serializers.IntegerField()
    unit_price = serializers.IntegerField()