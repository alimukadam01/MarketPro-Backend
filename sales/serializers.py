from django.db import transaction
from rest_framework import serializers

from core.serializers import SimpleUserSerializer
from root.models import Location
from root.serializers import (
    CustomerSerializer, SimpleBusinessSerializer, SimpleCustomerSerializer, SimpleProductSerializer, SimpleSupplierSerializer, 
    SupplierSerializer, BaseItemSerializer
)
from inventory.models import InventoryItem
from .models import PurchaseInvoice, PurchaseInvoiceItem, SalesInvoice, SalesInvoiceItem
from .utils import (
    checkPurchaseInvoiceItemFields, 
    checkPurchaseInvoiceCreateFields,
    checkSalesInvoiceItemCreateFields,
    updateInventoryOnSale
)

class PurchaseInvoiceItemSerializer(BaseItemSerializer):

    purchase_invoice = serializers.PrimaryKeyRelatedField(read_only=True) 
    product = SimpleProductSerializer()
    unit_cost = serializers.FloatField()
    quantity_received = serializers.IntegerField(default=True)
    is_restocked = serializers.BooleanField(read_only=True)
    is_partially_restocked = serializers.BooleanField(read_only=True)
    
    def update(self, instance, validated_data):
        
        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        instance.save()
        return instance


class PurchaseInvoiceItemCreateSerializer(BaseItemSerializer):
    unit_cost = serializers.FloatField()
    quantity_received = serializers.IntegerField(default=True)

    def validate(self, attrs):
        checkPurchaseInvoiceItemFields(attrs, self.context)
        return super().validate(attrs)

    def create(self, validated_data):
        try:
            item = PurchaseInvoiceItem.objects.get(
                purchase_invoice_id=self.context['purchase_invoice_id'],
                business_id=self.context['business_id']
            )
            item.quantity += validated_data['quantity']
            item.unit_cost = validated_data['unit_cost']
            item.quantity_received += validated_data['quantity_received']
            item.save()

            return item

        except PurchaseInvoiceItem.DoesNotExist:
            return PurchaseInvoiceItem.objects.create(
                purchase_invoice_id = self.context['purchase_invoice_id'],
                business_id = self.context['business_id'],
                **validated_data
            )
    

class PurchaseInvoiceItemUpdateSerializer(serializers.Serializer):
    purchase_invoice = serializers.PrimaryKeyRelatedField(read_only=True)
    quantity = serializers.IntegerField()
    unit_cost = serializers.FloatField()
    quantity_received = serializers.IntegerField(default=True)

    def validate(self, attrs):
        checkPurchaseInvoiceItemFields(attrs, self.context)
        return super().validate(attrs)

    def update(self, instance, validated_data):
        
        for attr, value in self.validated_data.items():
            setattr(instance, attr, value)

        instance.save()
        return instance


class SimplePurchaseInvoiceItemSerializer(serializers.ModelSerializer):

    product = SimpleProductSerializer(read_only=True)

    class Meta:
        model = PurchaseInvoiceItem
        fields = [
            'id', 'purchase_invoice', 'product', 'unit_cost', 'quantity' ,'track_code', 
            'notes', 'quantity_received', 'is_restocked', 'is_partially_restocked'
        ]


class PurchaseInvoiceSerializer(serializers.ModelSerializer):

    invoice_items = SimplePurchaseInvoiceItemSerializer(many=True, read_only=True)
    # business = SimpleBusinessSerializer(read_only=True)
    # supplier = SupplierSerializer(read_only=True)
    created_by = SimpleUserSerializer(read_only=True)

    class Meta:
        model = PurchaseInvoice
        fields = [
            'id', 'invoice_number', 'business', 'supplier', 
            'created_at', 'updated_at', 'date_due', 'status', 'payment_status', 
            'sub_total', 'tax',  'amount_paid', 'total', 'delivery', 'created_by',
            'notes', 'invoice_items', 'is_restocked', 'is_partially_restocked'
        ]


class SimplePurchaseInvoiceSerializer(serializers.ModelSerializer):

    supplier = SimpleSupplierSerializer(read_only=True, required=False)
    total_items = serializers.SerializerMethodField()

    def get_total_items(self, obj):
        return len(obj.invoice_items.all())

    class Meta:
        model = PurchaseInvoice
        fields = [
            'id', 'invoice_number', 'supplier', 
            'created_at', 'date_due', 'status', 
            'payment_status', 'sub_total', 'tax', 'total', 
            'delivery', 'total_items'
        ]


class PurchaseInvoiceCreateSerializer(serializers.ModelSerializer):

    business = SimpleBusinessSerializer(read_only=True)

    class Meta:
        model = PurchaseInvoice
        fields = [
            'id', 'invoice_number', 'business', 'supplier',
            'created_at', 'updated_at', 'date_due', 'status', 
            'payment_status', 'tax', 'delivery', 'notes'
        ]

    def validate(self, attrs):
        checkPurchaseInvoiceCreateFields(attrs)
        return super().validate(attrs)

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
            'status', 'payment_status', 'sub_total', 'tax', 'total', 'delivery', 'notes', 
        ]


    def update(self, instance, validated_data):
        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        instance.save()
        return instance


class PurchaseInvoiceAndItemsCreateSerializer(serializers.ModelSerializer):
    items = serializers.ListField()

    def save(self, **kwargs):
        items = self.validated_data.pop('items')

        print()

        try:
            with transaction.atomic():
                purchase_invoice = PurchaseInvoice.objects.create(
                    business_id = self.context['business_id'],
                    created_by_id = self.context['user_id'],
                    **self.validated_data
                )
            
                invoice_items = [PurchaseInvoiceItem(
                    business_id = self.context['business_id'],
                    purchase_invoice_id = purchase_invoice.id,
                    product_id = item['product_id'],
                    quantity = item['quantity'],
                    unit_cost = item['unit_cost']
                ) for item in items]
                invoice_items = PurchaseInvoiceItem.objects.bulk_create(invoice_items)

            purchase_invoice.adjust_totals()
            return purchase_invoice
        
        except Exception as error:
            print(error)
            return None
        
    class Meta:
        model = PurchaseInvoice
        fields = [
            'invoice_number',
            'supplier',
            'notes',
            'amount_paid',
            'date_due',
            'status',
            'payment_status',
            'tax',
            'delivery',
            'items'
        ]


class PurchaseInvoiceAndItemsUpdateSerializer(serializers.ModelSerializer):
    items = serializers.ListField()

    def save(self, **kwargs):
        items = self.validated_data.pop('items')

        try:
            with transaction.atomic():
                for attr, value in self.validated_data.items():
                    setattr(self.instance, attr, value)

                self.instance.save()

                existing_items = PurchaseInvoiceItem.objects.filter(
                    purchase_invoice_id = self.instance.id
                )
                existing_items_map = {
                    item.id: item for item in existing_items
                }
                existing_item_ids = set(existing_items.values_list('id', flat=True))

                new_items = []
                updated_items = []

                for item in items:
                    print(item)
                    if item['id'] in existing_item_ids:
                        invoice_item = existing_items_map.get(item['id'])
                        invoice_item.product_id = item['product_id']
                        invoice_item.quantity = item['quantity']
                        invoice_item.unit_cost = item['unit_cost']
                        updated_items.append(invoice_item)
                    else:
                        new_items.append(PurchaseInvoiceItem(
                            business_id = self.context['business_id'],
                            purchase_invoice = self.instance,
                            product_id = item['product_id'],
                            quantity = item['quantity'],
                            unit_cost = item['unit_cost']
                        ))

                if new_items:
                    print("Creating new items:", new_items)
                    for item in new_items:
                        new_item = item.save()
                        print(new_item)

                
                if updated_items:
                    PurchaseInvoiceItem.objects.bulk_update(updated_items, [
                        'product', 'quantity', 'unit_cost'
                    ])

                updated_ids = [item.id for item in updated_items]
                existing_items = existing_items.exclude(id__in=updated_ids)
                existing_items.delete()
                
            self.instance.adjust_totals()
            return self.instance
    
        except Exception as error:
            print(error)
            return None
        
    class Meta:
        model = PurchaseInvoice
        fields = [
            'invoice_number',
            'supplier',
            'notes',
            'date_due',
            'delivery',
            'status',
            'payment_status',
            'tax',
            'amount_paid',
            'items'
        ]


class SalesInvoiceItemSerializer(serializers.ModelSerializer):

    product = SimpleProductSerializer(read_only=True)

    class Meta:
        model = SalesInvoiceItem
        fields = [
            'id', 'sales_invoice', 'product', 'unit_price', 'quantity', 'quantity_received', 'track_code', 
            'notes', 'quantity_received', 'is_deducted', 'is_partially_deducted'
        ]

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
            'id', 'sales_invoice', 'product', 'unit_price', 'quantity', 
            'quantity_received', 'discount', 'is_deducted', 'is_partially_deducted'    
        ]


class SalesInvoiceItemCreateSerializer(BaseItemSerializer):
    unit_price = serializers.FloatField()
    quantity_received = serializers.IntegerField(default=True)
    discount = serializers.JSONField(default=dict)

    def validate(self, attrs):
        checkSalesInvoiceItemCreateFields(attrs)
        return super().validate(attrs)

    def create(self, validated_data):

        return SalesInvoiceItem.objects.create(
            sales_invoice_id = self.context['sales_invoice_id'],
            business_id = self.context['business_id'],
            **validated_data
        )


class SalesInvoiceItemUpdateSerializer(serializers.Serializer):
    sales_invoice = serializers.PrimaryKeyRelatedField(read_only=True)
    quantity = serializers.IntegerField()
    unit_price = serializers.FloatField()
    quantity_received = serializers.IntegerField(default=True)

    def update(self, instance, validated_data):
        
        for attr, value in self.validated_data.items():
            setattr(instance, attr, value)

        instance.save()
        return instance


class SalesInvoiceSerializer(serializers.ModelSerializer):

    invoice_items = SimpleSalesInvoiceItemSerializer(many=True, read_only=True)
    customer = SimpleCustomerSerializer(read_only=True)

    class Meta:
        model = SalesInvoice
        fields = [
            'id', 'invoice_number', 'business', 'customer', 'date_issued', 'date_due', 'payment_status',
            'status', 'sub_total', 'tax', 'discount', 'total', 'created_by', 'created_at',
            'notes', 'is_deducted', 'is_partially_deducted', 'invoice_items'
        ]


class SimpleSalesInvoiceSerializer(serializers.ModelSerializer):

    customer = SimpleCustomerSerializer(read_only=True)
    total_items = serializers.SerializerMethodField()

    def get_total_items(self, obj):

        return len(obj.invoice_items.all())

    class Meta:
        model = SalesInvoice
        fields = [
            'id', 'invoice_number', 'customer', 'date_issued', 'date_due',
            'payment_status', 'status', 'sub_total', 'tax', 'discount', 'total', 'total_items'
        ]


class SalesInvoiceCreateSerializer(serializers.ModelSerializer):

    business = SimpleBusinessSerializer(read_only=True)
    
    class Meta:
        model = SalesInvoice
        fields = [
            'id', 'invoice_number', 'business', 'customer', 'date_issued', 'date_due',
            'payment_status', 'status', 'tax', 'discount', 'notes'
        ]

    def validate(self, attrs):
        super().validate(attrs)

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
            'payment_status', 'status', 'sub_total', 'tax', 'discount', 'total', 'notes'
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
    

class SalesInvoiceAndItemsCreateSerializer(serializers.ModelSerializer):
    
    items = serializers.ListField()

    def save(self, **kwargs):
        items = self.validated_data.pop('items')

        try:
            with transaction.atomic():
                sales_invoice = SalesInvoice.objects.create(
                    business_id = self.context['business_id'],
                    created_by_id = self.context['user_id'],
                    **self.validated_data
                )

            
                invoice_items = [SalesInvoiceItem(
                    business_id = self.context['business_id'],
                    sales_invoice_id = sales_invoice.id,
                    product_id = item['product_id'],
                    quantity = item['quantity'],
                    unit_price = item['unit_price']
                ) for item in items]
                invoice_items = SalesInvoiceItem.objects.bulk_create(invoice_items)
            sales_invoice.adjust_totals()
            updateInventoryOnSale(sales_invoice)
            return sales_invoice
        
        except Exception as error:
            print(error)
            return None
        
    class Meta:
        model = SalesInvoice
        fields = [
            'invoice_number',
            'customer',
            'notes',
            'date_issued',
            'date_due',
            'discount',
            'tax',
            'payment_status',
            'status',
            'items'
        ]


class SalesInvoiceAndItemsUpdateSerializer(serializers.ModelSerializer):
    

    items = serializers.ListField()

    def save(self, **kwargs):
        items = self.validated_data.pop('items')

        try:
            # with transaction.atomic():
            for attr, value in self.validated_data.items():
                setattr(self.instance, attr, value)

            self.instance.save()

            existing_items = SalesInvoiceItem.objects.filter(
                sales_invoice_id = self.instance.id
            )
            existing_items_map = {
                item.id: item for item in existing_items
            }
            existing_item_ids = set(existing_items.values_list('id', flat=True))

            new_items = []
            updated_items = []

            for item in items:
                print(item)
                if item['id'] in existing_item_ids:
                    invoice_item = existing_items_map.get(item['id'])
                    invoice_item.product_id = item['product_id']
                    invoice_item.quantity = item['quantity']
                    invoice_item.unit_price = item['unit_price']
                    updated_items.append(invoice_item)
                else:
                    new_items.append(SalesInvoiceItem(
                        business_id = self.context['business_id'],
                        sales_invoice = self.instance,
                        product_id = item['product_id'],
                        quantity = item['quantity'],
                        unit_price = item['unit_price']
                    ))

            if new_items:
                print("Creating new items:", new_items)
                for item in new_items:
                    for attr, value in item.__dict__.items():
                        print(f"{attr}: {value}")

                new_items = SalesInvoiceItem.objects.bulk_create(new_items)
            
            if updated_items:
                SalesInvoiceItem.objects.bulk_update(updated_items, [
                    'product', 'quantity', 'unit_price'
                ])

            updated_ids = [item.id for item in updated_items]
            existing_items = existing_items.exclude(id__in=updated_ids)
            existing_items.delete()
            
            print(new_items)
            self.instance.adjust_totals()
            updateInventoryOnSale(self.instance)
            return self.instance
        
        except Exception as error:
            print(error)
            return None
        
    class Meta:
        model = SalesInvoice
        fields = [
            'invoice_number',
            'customer',
            'notes',
            'date_issued',
            'date_due',
            'discount',
            'tax',
            'payment_status',
            'status',
            'items'
        ]

