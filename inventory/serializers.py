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
    today, unless the date was given (an import, or a count typed in after
    the day it was taken)."""
    if quantity_set and not values.get('stock_counted_at'):
        values['stock_counted_at'] = timezone.localdate()
    return values


# Only `quantity` is typed. On hand and reserved are the server's:
#   quantity_on_hand = quantity - quantity_reserved
# with reserved at 0, since nothing reserves stock yet (SalesReservation is
# dead). Every automatic writer - sales, purchases, cancels, returns, the
# backlog count, RestockSerializer - already moves quantity and on hand
# together; these two endpoints were the only place a person could make them
# differ, and on hand is what availability, low stock and the
# insufficient-stock check read while quantity is what stock value reads.
# Kept here rather than in InventoryItem.save() on purpose: the import
# commands create rows one by one and must keep writing their own figures.
DERIVED_STOCK_FIELDS = ('quantity_on_hand', 'quantity_reserved')


def derive_on_hand(values, reserved=0):
    """Set on hand from the typed quantity, when this write has one."""
    if 'quantity' in values:
        values['quantity_on_hand'] = values['quantity'] - (reserved or 0)
    return values


DATE_NEEDS_QUANTITY = (
    'The count date can only be set together with a new quantity - it says '
    'when that figure was counted.')


def validate_count_date(value):
    """
    A count can be dated today or earlier, never later. A future count would
    treat every sale and purchase until then as already in it
    (sales/stock_rule.py), and the stock would silently stop moving.
    """
    if value and value > timezone.localdate():
        raise serializers.ValidationError(
            'A stock count cannot be dated in the future.')
    return value


class InventoryItemCreateSerializer(serializers.ModelSerializer):

    def validate_stock_counted_at(self, value):
        return validate_count_date(value)

    def validate(self, attrs):
        if attrs.get('stock_counted_at') and 'quantity' not in attrs:
            raise serializers.ValidationError(
                {'stock_counted_at': DATE_NEEDS_QUANTITY})
        return attrs

    def create(self, validated_data):
        """
        create(), not save(): overriding save() never set self.instance, so
        DRF rendered the submitted payload back as the response instead of
        the saved row - and on hand, which the server sets, was missing from
        it. Same fault as accounts.serializers.TransactionCreateSerializer.
        """
        # A new row reserves nothing, so on hand is the quantity.
        values = derive_on_hand(dict(validated_data))
        values = stamp_count(values, 'quantity' in values)
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
        # Returned, never taken from the client: see DERIVED_STOCK_FIELDS.
        read_only_fields = DERIVED_STOCK_FIELDS


class InventoryItemUpdateSerializer(serializers.ModelSerializer):
    """
    A changed quantity is a count (§8), dated today unless the user picks an
    earlier day - a count taken at closing and typed in next morning. The
    date can only move with a new quantity, and never into the future. On
    hand follows the quantity (DERIVED_STOCK_FIELDS); reserved is not
    writable.

    carry_forward: for a count dated before today, add what has already moved
    this stock since that day (sold, received, returned - see
    sales.stock_rule.movements_after) to the typed figure. The user ticks it
    on the edit screen after seeing those movements; left unticked, the typed
    figure is saved as it is.
    """

    carry_forward = serializers.BooleanField(
        write_only=True, required=False, default=False)

    def is_count(self, attrs):
        """Only a changed quantity is a count; a price edit is not (§8)."""
        return 'quantity' in attrs and attrs['quantity'] != self.instance.quantity

    def validate_stock_counted_at(self, value):
        return validate_count_date(value)

    def validate(self, attrs):
        if ('stock_counted_at' in attrs
                and attrs['stock_counted_at'] != self.instance.stock_counted_at
                and not self.is_count(attrs)):
            raise serializers.ValidationError(
                {'stock_counted_at': DATE_NEEDS_QUANTITY})
        return attrs

    def save(self, **kwargs):
        from sales.stock_rule import movements_after

        values = dict(self.validated_data)
        carry_forward = values.pop('carry_forward', False)
        counted = self.is_count(values)
        values = stamp_count(values, counted)

        if not counted:
            # Not a count, so nothing may re-date the last one, and on hand
            # is left exactly as it was.
            values.pop('stock_counted_at', None)
            values.pop('quantity', None)
        else:
            if carry_forward and values['stock_counted_at'] < timezone.localdate():
                moved = movements_after(
                    self.instance.business_id, self.instance.product_id,
                    values['stock_counted_at'],
                    counted=self.instance.stock_counted_at)
                values['quantity'] += moved['net']
            derive_on_hand(values, self.instance.quantity_reserved)

        for attr, value in values.items():
            setattr(self.instance, attr, value)

        self.instance.save()
        return self.instance

    class Meta:
        model = InventoryItem
        fields = [
            'id', 'product', 'location', 'quantity', 'quantity_on_hand', 'quantity_reserved','track_code',
            'notes', 'unit_cost', 'unit_price', 'reorder_level', 'stock_counted_at',
            'carry_forward',
        ]
        # Returned, never taken from the client: see DERIVED_STOCK_FIELDS.
        read_only_fields = DERIVED_STOCK_FIELDS


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