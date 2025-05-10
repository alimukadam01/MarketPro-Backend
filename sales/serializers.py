from django.db import transaction
from rest_framework import serializers
from root.models import Location
from inventory.models import InventoryItem
from .models import PurchaseInvoice, PurchaseInvoiceItem, SalesInvoice, SalesInvoiceItem
from core.serializers import SimpleUserSerializer
from root.serializers import (
    CustomerSerializer, SimpleBusinessSerializer, SimpleProductSerializer, 
    SupplierSerializer, BaseItemSerializer
)

class PurchaseInvoiceItemSerializer(BaseItemSerializer):

    purchase_invoice = serializers.PrimaryKeyRelatedField(read_only=True) 
    unit_cost = serializers.FloatField()

    def create(self, validated_data):
        return PurchaseInvoiceItem.objects.create(
            purchase_invoice_id = self.context['purchase_invoice_id'],
            business_id = self.context['business_id'],
            **validated_data
        )
    
    def update(self, instance, validated_data):
        
        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        instance.save()
        return instance


class SimplePurchaseInvoiceItemSerializer(serializers.ModelSerializer):

    product = SimpleProductSerializer(read_only=True)

    class Meta:
        model = PurchaseInvoiceItem
        fields = [
            'id', 'product', 'unit_cost', 'quantity' ,'track_code', 'notes'
        ]


class PurchaseInvoiceSerializer(serializers.ModelSerializer):

    business = SimpleBusinessSerializer(read_only=True)
    invoice_items = SimplePurchaseInvoiceItemSerializer(many=True, read_only=True)
    supplier = SupplierSerializer(read_only=True)
    created_by = SimpleUserSerializer(read_only=True)

    class Meta:
        model = PurchaseInvoice
        fields = [
            'id', 'invoice_number', 'business', 'supplier', 
            'created_at', 'updated_at', 'date_due', 'status', 
            'sub_total', 'tax',  'total', 'delivery', 'created_by',
            'notes', 'invoice_items'
        ]


class PurchaseInvoiceCreateSerializer(serializers.ModelSerializer):

    business = SimpleBusinessSerializer(read_only=True)

    class Meta:
        model = PurchaseInvoice
        fields = [
            'id', 'invoice_number', 'business', 'supplier',
            'created_at', 'updated_at', 'date_due', 'status',
            'sub_total', 'tax', 'total', 'delivery', 'notes'
        ]

    def save(self, **kwargs):
        return PurchaseInvoice.objects.create(
            business_id = self.context['business_id'],
            created_by_id = self.context['user_id'],
            **self.validated_data
        )
    

class PurchaseInvoiceUpdateSerializer(serializers.ModelSerializer):

    class Meta:
        model = PurchaseInvoice
        fields = [
            'id', 'invoice_number', 'supplier', 'date_due', 
            'status', 'sub_total', 'tax', 'total', 'delivery', 'notes'
        ]


    def update(self, instance, validated_data):
        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        instance.save()
        return instance
    

class SalesInvoiceItemSerializer(BaseItemSerializer):

    sales_invoice = serializers.PrimaryKeyRelatedField(read_only=True)
    unit_price = serializers.FloatField()
    discount = serializers.IntegerField(required = False, allow_null = True)

    def create(self, validated_data):

        inventory_item = InventoryItem.objects.get(
            inventory_id = self.context['inventory_id'], 
            product_id = self.validated_data['product'].id
        )

        if inventory_item.quantity_on_hand < self.validated_data['quantity']:
            return None

        return SalesInvoiceItem.objects.create(
            sales_invoice_id = self.context['sales_invoice_id'],
            business_id = self.context['business_id'],
            **validated_data
        )
    
    def update(self, instance, validated_data):
        
        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        instance.save()
        return instance
    

class SimpleSalesInvoiceItemSerializer(serializers.ModelSerializer):

    product = SimpleProductSerializer(read_only=True)

    class Meta:
        model = SalesInvoiceItem
        fields = [
            'id', 'sales_invoice', 'product', 'unit_price', 'quantity', 'discount'    
        ]


class SalesInvoiceSerializer(serializers.ModelSerializer):

    invoice_items = SimpleSalesInvoiceItemSerializer(many=True, read_only=True)
    customer = CustomerSerializer(read_only=True)
    business = SimpleBusinessSerializer(read_only=True)
    
    class Meta:
        model = SalesInvoice
        fields = [
            'id', 'invoice_number', 'business', 'customer', 'date_issued', 'date_due',
            'status', 'sub_total', 'tax', 'discount', 'total', 'created_by', 'created_at',
            'notes', 'invoice_items'
        ]


class SalesInvoiceCreateSerializer(serializers.ModelSerializer):

    business = SimpleBusinessSerializer(read_only=True)
    
    class Meta:
        model = SalesInvoice
        fields = [
            'id', 'invoice_number', 'business', 'customer', 'date_issued', 'date_due',
            'status', 'sub_total', 'tax', 'discount', 'total', 'notes'
        ]

    def save(self, **kwargs):
        return SalesInvoice.objects.create(
            business_id = self.context['business_id'],
            created_by_id = self.context['user_id'],
            **self.validated_data
        )


class SalesInvoiceUpdateSerializer(serializers.ModelSerializer):

    class Meta:
        model = SalesInvoice
        fields = [
            'id', 'invoice_number', 'customer', 'date_issued', 'date_due',
            'status', 'sub_total', 'tax', 'discount', 'total', 'notes'
        ]
    

    def save(self, **kwargs):
        
        for attr, value in self.validated_data.items():
            setattr(self.instance, attr, value)

        self.instance.save()
        return self.instance


class RestockSerializer(serializers.Serializer):

    location = serializers.PrimaryKeyRelatedField(queryset=Location.objects.none())

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        business_id = self.context.get('business_id')
        if business_id and self.fields.get('location'):
            self.fields['location'].queryset = Location.objects.filter(business_id=business_id)


    def save(self, **kwargs):
        
        purchase_invoice_items = PurchaseInvoiceItem.objects.filter(
            purchase_invoice_id=self.context['purchase_invoice_id']
        )

        if not purchase_invoice_items:
            return False
        
        invoice_items_map = {
            item.product.id: item for item in purchase_invoice_items
        }

        product_ids = [item.product.id for item in purchase_invoice_items]
        product_ids = set(purchase_invoice_items.values_list('product_id', flat=True))
        inventory_items = InventoryItem.objects.filter(
            inventory_id = self.context['inventory_id'],
            product_id__in = product_ids    
        )

        existing_products = set(inventory_items.values_list('product_id', flat=True))
        new_inventory_products = product_ids - existing_products

        new_inventory_items = [InventoryItem(
            inventory_id = self.context['inventory_id'],
            business_id = self.context['business_id'],
            product_id = item.product.id,
            location = self.validated_data['location'],
            quantity = item.quantity,
            track_code = item.track_code,
            notes = item.notes,
            quantity_on_hand = item.quantity,
            unit_cost = item.unit_cost
        ) for item in purchase_invoice_items if item.product.id in new_inventory_products]

        for item in inventory_items:
            invoice_item = invoice_items_map.get(item.product.id)
            if invoice_item:
                item.location = self.validated_data['location']
                item.quantity += invoice_item.quantity
                item.quantity_on_hand += invoice_item.quantity
                item.unit_cost = invoice_item.unit_cost
                item.notes = invoice_item.notes

        with transaction.atomic():
            InventoryItem.objects.bulk_create(new_inventory_items)
            InventoryItem.objects.bulk_update(inventory_items, [
                'location', 'quantity', 'quantity_on_hand',
                'unit_cost', 'notes'
            ])

        return True


        
