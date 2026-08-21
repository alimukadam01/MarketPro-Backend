from django.db import transaction
from django.db.models import Q, Sum
from rest_framework import serializers

from projects.models import ProjectPurchaseInvoice, ProjectSalesInvoice
from core.serializers import SimpleUserSerializer
from root.models import Location
from root.serializers import (
    BusinessSerializer, ProductVariantSerializer, SimpleBusinessSerializer,
    SimpleCustomerSerializer, SimpleSupplierSerializer,
    SupplierSerializer, BaseItemSerializer
)
from inventory.models import InventoryItem
from .models import PurchaseInvoice, PurchaseInvoiceItem, PurchaseQuotation, PurchaseQuotationItem, PurchaseReceipt, SalesInvoice, SalesInvoiceItem, ReturnedItem, SalesReceipt
from .utils import (
    checkPurchaseInvoiceItemFields,
    checkPurchaseInvoiceCreateFields,
    checkSalesInvoiceItemCreateFields,
    updateInventoryOnSale
)


# A payment holds its share of the invoice while it is cleared or still
# pending. A bounced one never arrives, so it frees its share up again.
# Receipts with no transaction behind them predate the accounting module.
LIVE_RECEIPT = (
    Q(transaction_record__isnull=True) |
    Q(transaction_record__status__in=['C', 'PEN'])
)


class PaymentReceiptSerializer(serializers.Serializer):

    id = serializers.IntegerField(read_only=True)
    amount = serializers.FloatField()
    desc = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    account = serializers.SerializerMethodField()
    payment_method = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)

    def get_account(self, receipt):
        """
        The receipt carries the context; the account lives on the transaction
        the accounts module mirrors it into.
        """
        transaction = getattr(receipt, 'transaction_record', None)
        if not transaction or not transaction.account_id:
            return None

        return {
            'id': transaction.account_id,
            'name': transaction.account.name,
            'type': transaction.account.type,
        }

    def get_payment_method(self, receipt):
        transaction = getattr(receipt, 'transaction_record', None)
        return transaction.payment_method if transaction else None

    def get_status(self, receipt):
        # Cleared, pending or bounced. Only cleared money pays the invoice.
        transaction = getattr(receipt, 'transaction_record', None)
        return transaction.status if transaction else None


class PaymentReceiptCreateSerializer(PaymentReceiptSerializer):
    """
    Adds the money details a receipt does not itself store. A signal mirrors
    every receipt into a Transaction; these fields refine that record.
    """

    account = serializers.IntegerField(required=False, allow_null=True)
    payment_method = serializers.CharField(required=False, allow_null=True)
    date = serializers.DateField(required=False, allow_null=True)
    cheque_number = serializers.CharField(
        required=False, allow_null=True, allow_blank=True)
    cheque_due_date = serializers.DateField(required=False, allow_null=True)

    MONEY_FIELDS = [
        'account', 'payment_method', 'date', 'cheque_number', 'cheque_due_date'
    ]

    def pop_money_details(self):
        details = {}
        for field in self.MONEY_FIELDS:
            value = self.validated_data.pop(field, None)
            if value:
                details['account_id' if field == 'account' else field] = value
        return details

    def apply_money_details(self, receipt, money_details):
        if not money_details:
            return receipt

        try:
            transaction = receipt.transaction_record
        except Exception as error:
            print(error)
            return receipt

        # A cheque stays pending until it clears, so it moves no money yet.
        if money_details.get('payment_method') == 'cheque':
            money_details['status'] = 'PEN'

        for attr, value in money_details.items():
            setattr(transaction, attr, value)
        transaction.save(update_fields=list(money_details.keys()))

        return receipt


class PurchaseInvoiceItemSerializer(BaseItemSerializer):

    purchase_invoice = serializers.PrimaryKeyRelatedField(read_only=True)
    product = ProductVariantSerializer()
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
                purchase_invoice_id=self.context['purchase_invoice_id'],
                business_id=self.context['business_id'],
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

    product = ProductVariantSerializer(read_only=True)

    class Meta:
        model = PurchaseInvoiceItem
        fields = [
            'id', 'purchase_invoice', 'product', 'unit_cost', 'quantity', 'track_code',
            'notes', 'quantity_received', 'is_restocked', 'is_partially_restocked'
        ]


class ProjectPurchaseInvoiceLinker(serializers.ModelSerializer):

    class Meta:
        model = ProjectSalesInvoice
        fields = ['id', 'project']


class PurchaseInvoiceSerializer(serializers.ModelSerializer):

    invoice_items = SimplePurchaseInvoiceItemSerializer(
        many=True, read_only=True)
    created_by = SimpleUserSerializer(read_only=True)
    projects = ProjectPurchaseInvoiceLinker(many=True)
    payment_receipts = PaymentReceiptSerializer(many=True, read_only=True)

    class Meta:
        model = PurchaseInvoice
        fields = [
            'id', 'invoice_number', 'business', 'supplier',
            'created_at', 'updated_at', 'date_due', 'status', 'payment_status',
            'sub_total', 'tax',  'amount_paid', 'total', 'delivery', 'created_by',
            'notes', 'invoice_items', 'is_restocked', 'is_partially_restocked', 'projects', 'payment_receipts'
        ]


class SimplePurchaseInvoiceSerializer(serializers.ModelSerializer):

    supplier = SimpleSupplierSerializer(read_only=True, required=False)
    total_items = serializers.SerializerMethodField()
    projects = ProjectPurchaseInvoiceLinker(many=True)

    def get_total_items(self, obj):

        if type(obj) == PurchaseInvoice:
            return obj.invoice_items.count()

        if type(obj) == ProjectPurchaseInvoice:
            return obj.purchase_invoice.invoice_items.count()

    class Meta:
        model = PurchaseInvoice
        fields = [
            'id', 'invoice_number', 'supplier',
            'created_at', 'date_due', 'status',
            'payment_status', 'sub_total', 'tax', 'total',
            'delivery', 'total_items', 'projects'
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
            business_id=self.context['business_id'],
            created_by_id=self.context['user_id'],
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
        instance.adjust_totals()
        return instance


class PurchaseInvoiceAndItemsCreateSerializer(serializers.ModelSerializer):
    items = serializers.ListField()
    project = serializers.IntegerField(required=False, allow_null=True)
    amount_paid = serializers.FloatField(
        required=False, allow_null=True, write_only=True)

    def save(self, **kwargs):
        items = self.validated_data.pop('items')
        project_id = self.validated_data.pop('project', None)
        project_id = int(project_id) if project_id else project_id
        amount_paid = self.validated_data.pop('amount_paid', None)

        try:
            with transaction.atomic():
                purchase_invoice = PurchaseInvoice.objects.create(
                    business_id=self.context['business_id'],
                    created_by_id=self.context['user_id'],
                    **self.validated_data
                )

                invoice_items = [PurchaseInvoiceItem(
                    business_id=self.context['business_id'],
                    purchase_invoice_id=purchase_invoice.id,
                    product_id=item['product_id'],
                    quantity=item['quantity'],
                    unit_cost=item['unit_cost']
                ) for item in items]
                invoice_items = PurchaseInvoiceItem.objects.bulk_create(
                    invoice_items)

                if project_id:
                    ProjectPurchaseInvoice.objects.create(
                        project_id=project_id,
                        purchase_invoice=purchase_invoice
                    )

            purchase_invoice.adjust_totals()

            # Money paid at the time of purchase becomes a payment record;
            # the accounts signal mirrors it into a transaction and
            # payment_status derives from it.
            if amount_paid and float(amount_paid) > 0:
                PurchaseReceipt.objects.create(
                    purchase_invoice_id=purchase_invoice.id,
                    amount=min(
                        float(amount_paid),
                        purchase_invoice.total or float(amount_paid)
                    ),
                )

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
            'items',
            'project'
        ]


class PurchaseInvoiceAndItemsUpdateSerializer(serializers.ModelSerializer):
    items = serializers.ListField()
    project = serializers.IntegerField(required=False, allow_null=True)

    def save(self, **kwargs):
        items = self.validated_data.pop('items')
        project_id = self.validated_data.pop('project', None)
        project_id = int(project_id) if project_id else project_id

        try:
            with transaction.atomic():
                for attr, value in self.validated_data.items():
                    setattr(self.instance, attr, value)

                self.instance.save()

                existing_items = PurchaseInvoiceItem.objects.filter(
                    purchase_invoice_id=self.instance.id
                )
                existing_items_map = {
                    item.id: item for item in existing_items
                }
                existing_item_ids = set(
                    existing_items.values_list('id', flat=True))

                new_items = []
                updated_items = []

                for item in items:
                    if item['id'] in existing_item_ids:
                        invoice_item = existing_items_map.get(item['id'])
                        invoice_item.product_id = item['product_id']
                        invoice_item.quantity = item['quantity']
                        invoice_item.unit_cost = item['unit_cost']
                        updated_items.append(invoice_item)
                    else:
                        new_items.append(PurchaseInvoiceItem(
                            business_id=self.context['business_id'],
                            purchase_invoice=self.instance,
                            product_id=item['product_id'],
                            quantity=item['quantity'],
                            unit_cost=item['unit_cost']
                        ))

                if new_items:
                    new_items = PurchaseInvoiceItem.objects.bulk_create(
                        new_items)

                if updated_items:
                    PurchaseInvoiceItem.objects.bulk_update(updated_items, [
                        'product', 'quantity', 'unit_cost'
                    ])

                updated_ids = [item.id for item in updated_items]
                new_ids = [item.id for item in new_items]
                existing_items = existing_items.exclude(
                    id__in=updated_ids+new_ids)
                existing_items.delete()

            self.instance.adjust_totals()
            self.instance.refresh_from_db()

            # Handle Projects Addition/Updation/Deletion Logic
            if not project_id:
                print(
                    "printing from ProjectPurchaseInvoice deletion block. Project ID is: ", project_id)
                ProjectPurchaseInvoice.objects.filter(
                    purchase_invoice=self.instance).delete()
            else:
                print(
                    "printing from ProjectPurchaseInvoice creation block. Project ID is: ", project_id)
                ProjectPurchaseInvoice.objects.filter(
                    purchase_invoice=self.instance).delete()
                ProjectPurchaseInvoice.objects.get_or_create(
                    project_id=project_id,
                    purchase_invoice=self.instance
                )

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
            'items',
            'project'
        ]


class PurchaseReceiptCreateSerializer(PaymentReceiptCreateSerializer):

    def is_valid(self, *, raise_exception=False):
        purchase_invoice = PurchaseInvoice.objects.get(
            id=self.context["purchase_invoice_id"]
        )

        # A pending cheque still occupies its share of the invoice; a bounced
        # one does not, so its amount can be paid again.
        total_paid = (
            purchase_invoice.payment_receipts
            .filter(LIVE_RECEIPT)
            .aggregate(total=Sum("amount"))["total"] or 0
        )

        if float(self.initial_data["amount"]) + total_paid > purchase_invoice.total:
            raise serializers.ValidationError({
                "amount": "accumulated amount cannot exceed invoice total"
            })

        return super().is_valid(raise_exception=raise_exception)

    def save(self, **kwargs):
        money_details = self.pop_money_details()
        receipt = PurchaseReceipt.objects.create(
            purchase_invoice_id=self.context['purchase_invoice_id'],
            **self.validated_data
        )
        return self.apply_money_details(receipt, money_details)


class SalesInvoiceItemSerializer(serializers.ModelSerializer):

    product = ProductVariantSerializer(read_only=True)

    class Meta:
        model = SalesInvoiceItem
        fields = [
            'id', 'sales_invoice', 'product', 'unit_price', 'quantity', 'returned_quantity', 'net_quantity',
            'track_code', 'notes', 'quantity_received', 'is_deducted', 'is_partially_deducted', 'is_returned'
        ]

    def update(self, instance, validated_data):

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        instance.save()
        return instance


class SimpleSalesInvoiceItemSerializer(serializers.ModelSerializer):

    product = ProductVariantSerializer(read_only=True)

    class Meta:
        model = SalesInvoiceItem
        fields = [
            'id', 'sales_invoice', 'product', 'unit_price', 'quantity', 'returned_quantity', 'net_quantity',
            'quantity_received', 'discount', 'is_deducted', 'is_partially_deducted', 'is_returned'
        ]


class BasicSalesInvoiceItemSerializer(serializers.ModelSerializer):
    product = ProductVariantSerializer(read_only=True)

    class Meta:
        model = SalesInvoiceItem
        fields = [
            'id', 'product', 'unit_price', 'quantity', 'returned_quantity', 'net_quantity'
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
            sales_invoice_id=self.context['sales_invoice_id'],
            business_id=self.context['business_id'],
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


class ProjectSalesInvoiceLinker(serializers.ModelSerializer):

    class Meta:
        model = ProjectSalesInvoice
        fields = ['id', 'project']


class SalesInvoiceSerializer(serializers.ModelSerializer):

    invoice_items = SimpleSalesInvoiceItemSerializer(many=True, read_only=True)
    customer = SimpleCustomerSerializer(read_only=True)
    projects = ProjectSalesInvoiceLinker(many=True)
    payment_receipts = PaymentReceiptSerializer(many=True)

    class Meta:
        model = SalesInvoice
        fields = [
            'id', 'invoice_number', 'business', 'customer', 'date_issued', 'date_due', 'payment_status',
            'status', 'sub_total', 'tax', 'discount', 'total', 'amount_paid' ,'created_by', 'created_at',
            'notes', 'is_deducted', 'is_partially_deducted', 'invoice_items', 'projects', 'payment_receipts'
        ]


class SimpleSalesInvoiceSerializer(serializers.ModelSerializer):

    customer = SimpleCustomerSerializer(read_only=True)
    total_items = serializers.SerializerMethodField()
    projects = ProjectSalesInvoiceLinker(many=True)

    def get_total_items(self, obj):

        if type(obj) == SalesInvoice:
            return obj.invoice_items.count()

        if type(obj) == ProjectSalesInvoice:
            return obj.sales_invoice.invoice_items.count()

    class Meta:
        model = SalesInvoice
        fields = [
            'id', 'invoice_number', 'customer', 'date_issued', 'date_due',
            'payment_status', 'status', 'sub_total', 'tax', 'discount', 'total', 'total_items', 'projects'
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
            business_id=self.context['business_id'],
            created_by_id=self.context['user_id'],
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


class CompleteSalesInvoiceItemSerializer(SimpleSalesInvoiceItemSerializer):
    sales_invoice = SimpleSalesInvoiceSerializer()


class RestockSerializer(serializers.Serializer):

    location = serializers.PrimaryKeyRelatedField(
        queryset=Location.objects.none())

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        business_id = self.context.get('business_id')
        if business_id and self.fields.get('location'):
            self.fields['location'].queryset = Location.objects.filter(
                business_id=business_id)

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
        product_ids = set(purchase_invoice_items.values_list(
            'product_id', flat=True))
        inventory_items = InventoryItem.objects.filter(
            inventory_id=self.context['inventory_id'],
            product_id__in=product_ids
        )

        existing_products = set(
            inventory_items.values_list('product_id', flat=True))
        new_inventory_products = product_ids - existing_products

        new_inventory_items = [InventoryItem(
            inventory_id=self.context['inventory_id'],
            business_id=self.context['business_id'],
            product_id=item.product.id,
            location=self.validated_data['location'],
            quantity=item.quantity,
            track_code=item.track_code,
            notes=item.notes,
            quantity_on_hand=item.quantity,
            unit_cost=item.unit_cost
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
    project = serializers.IntegerField(required=False, allow_null=True)
    amount_paid = serializers.FloatField(
        required=False, allow_null=True, write_only=True)

    def save(self, **kwargs):
        items = self.validated_data.pop('items')
        project_id = self.validated_data.pop('project', None)
        amount_paid = self.validated_data.pop('amount_paid', None)

        try:
            with transaction.atomic():
                sales_invoice = SalesInvoice.objects.create(
                    business_id=self.context['business_id'],
                    created_by_id=self.context['user_id'],
                    **self.validated_data
                )

                invoice_items = [SalesInvoiceItem(
                    business_id=self.context['business_id'],
                    sales_invoice_id=sales_invoice.id,
                    product_id=item['product_id'],
                    quantity=item['quantity'],
                    quantity_received=item['quantity'],
                    unit_price=item['unit_price']
                ) for item in items]
                SalesInvoiceItem.objects.bulk_create(invoice_items)

                if project_id:
                    ProjectSalesInvoice.objects.create(
                        project_id=project_id,
                        sales_invoice=sales_invoice
                    )

            sales_invoice.refresh_from_db()
            sales_invoice.adjust_totals()
            # No need to call this, it's called in adjust_totals.
            updateInventoryOnSale(sales_invoice)

            # Money received at the time of sale becomes a payment record;
            # the accounts signal mirrors it into a transaction and
            # payment_status derives from it.
            if amount_paid and float(amount_paid) > 0:
                SalesReceipt.objects.create(
                    sales_invoice_id=sales_invoice.id,
                    amount=min(
                        float(amount_paid),
                        sales_invoice.total or float(amount_paid)
                    ),
                )

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
            'items',
            'project',
            'amount_paid'
        ]


class SalesInvoiceAndItemsUpdateSerializer(serializers.ModelSerializer):

    items = serializers.ListField()
    project = serializers.IntegerField(required=False, allow_null=True)

    def save(self, **kwargs):
        items = self.validated_data.pop('items')
        project_id = self.validated_data.pop('project', None)
        project_id = int(project_id) if project_id else project_id

        try:
            with transaction.atomic():
                for attr, value in self.validated_data.items():
                    setattr(self.instance, attr, value)
                self.instance.save()

                existing_items = SalesInvoiceItem.objects.filter(
                    sales_invoice_id=self.instance.id
                )
                existing_items_map = {
                    item.id: item for item in existing_items
                }
                existing_item_ids = set(
                    existing_items.values_list('id', flat=True))

                new_items = []
                updated_items = []

                for item in items:
                    # this block essentially does nothing as invoice items
                    # can't be updated on the frontend currently.
                    # attr updation commented as functionality not on frontend yet.

                    if item['id'] in existing_item_ids:
                        invoice_item = existing_items_map.get(item['id'])
                        # invoice_item.product_id = item['product_id']
                        # invoice_item.quantity = item['quantity']
                        # invoice_item.unit_price = item['unit_price']
                        updated_items.append(invoice_item)
                    else:
                        new_items.append(SalesInvoiceItem(
                            business_id=self.context['business_id'],
                            sales_invoice=self.instance,
                            product_id=item['product_id'],
                            quantity=item['quantity'],
                            unit_price=item['unit_price']
                        ))

                if new_items:
                    new_items = SalesInvoiceItem.objects.bulk_create(new_items)

                # Items on the frontend can't be updated,
                # They can either be created or deleted.

                # if updated_items:
                #     SalesInvoiceItem.objects.bulk_update(updated_items, [
                #         'product', 'quantity', 'unit_price'
                #     ])

                updated_ids = [item.id for item in updated_items]
                new_ids = [item.id for item in new_items]
                existing_items = existing_items.exclude(
                    id__in=updated_ids+new_ids)
                existing_items.delete()

                self.instance.refresh_from_db()
                self.instance.adjust_totals()
                updateInventoryOnSale(self.instance)

                # Handle Projects Addition/Updation/Deletion Logic
                if not project_id:
                    print(
                        "printing from ProjectPurchaseInvoice deletion block. Project ID is: ", project_id)
                    ProjectSalesInvoice.objects.filter(
                        sales_invoice=self.instance).delete()
                else:
                    print(
                        "printing from ProjectPurchaseInvoice updation/creation block. Project ID is: ", project_id)
                    ProjectSalesInvoice.objects.filter(
                        sales_invoice=self.instance).exclude(project_id=project_id).delete()
                    ProjectSalesInvoice.objects.get_or_create(
                        project_id=project_id,
                        sales_invoice=self.instance
                    )

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
            'items',
            'project'
        ]


class SalesReceiptCreateSerializer(PaymentReceiptCreateSerializer):

    def is_valid(self, *, raise_exception=False):
        sales_invoice = SalesInvoice.objects.get(
            id=self.context["sales_invoice_id"]
        )

        # A pending cheque still occupies its share of the invoice; a bounced
        # one does not, so its amount can be paid again.
        total_recorded = (
            sales_invoice.payment_receipts
            .filter(LIVE_RECEIPT)
            .aggregate(total=Sum("amount"))["total"] or 0
        )

        if float(self.initial_data["amount"]) + total_recorded > sales_invoice.total:
            raise serializers.ValidationError({
                "amount": "accumulated amount cannot exceed invoice total"
            })

        return super().is_valid(raise_exception=raise_exception)

    def save(self, **kwargs):
        money_details = self.pop_money_details()
        receipt = SalesReceipt.objects.create(
            sales_invoice_id=self.context['sales_invoice_id'],
            **self.validated_data
        )
        return self.apply_money_details(receipt, money_details)


class ReturnedItemSerializer(serializers.ModelSerializer):

    invoice_item = CompleteSalesInvoiceItemSerializer(read_only=True)

    class Meta:
        model = ReturnedItem
        fields = [
            'id', 'invoice_item', 'is_damaged', 'reason', 'is_returned',
            'return_type', 'quantity', 'created_at', 'updated_at'
        ]


class ReturnedItemCreateUpdateSerializer(serializers.ModelSerializer):

    invoice_item = SimpleSalesInvoiceItemSerializer(read_only=True)

    class Meta:
        model = ReturnedItem
        fields = ['id', 'invoice_item', 'reason', 'quantity', 'is_damaged']

    def create(self, validated_data):

        invoice_item = SalesInvoiceItem.objects.get(
            id=self.context["invoice_item_id"])
        invoice_item.returned_quantity += validated_data['quantity']

        with transaction.atomic():
            returned_item = ReturnedItem.objects.create(
                business_id=self.context['business_id'],
                invoice_item_id=self.context["invoice_item_id"],
                **validated_data
            )
            invoice_item.save()

        return returned_item

    def update(self, instance: ReturnedItem, validated_data):

        old_quantity = instance.quantity

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        invoice_item = SalesInvoiceItem.objects.get(
            id=self.context["invoice_item_id"])
        invoice_item.returned_quantity -= old_quantity
        invoice_item.returned_quantity += instance.quantity

        with transaction.atomic():
            instance.save()
            invoice_item.save()

        return instance


class RecentSalesSerializer(serializers.ModelSerializer):

    class Meta:
        model = SalesInvoice
        fields = ['id', 'product_name']


class GenerateInvoiceSerializer(serializers.ModelSerializer):

    business = BusinessSerializer()
    customer = SimpleCustomerSerializer()
    invoice_items = BasicSalesInvoiceItemSerializer(many=True)
    amount_paid = serializers.FloatField(read_only=True)

    class Meta:
        model = SalesInvoice
        fields = [
            'id', 'created_at', 'date_issued', 'business', 'customer',
            'invoice_number', 'invoice_items', 'discount', 'tax', 'sub_total',
            'total', 'amount_paid'
        ]


class PurchaseQuotationItemSerializer(BaseItemSerializer):
    unit_price = serializers.FloatField()
    supplier = SupplierSerializer()
    is_fulfilled = serializers.BooleanField()


class SimplePurchaseQuotationItemSerializer(serializers.ModelSerializer):
    product = ProductVariantSerializer()
    supplier = SimpleSupplierSerializer()

    class Meta:
        model = PurchaseQuotationItem
        fields = ['id', 'purchase_quotation', 'supplier',
                  'product', 'quantity', 'unit_price', 'is_fulfilled']


class PurchaseQuotationItemCreateUpdateSerializer(serializers.ModelSerializer):

    def save(self, **kwargs):
        return PurchaseQuotationItem.objects.create(
            business_id=self.context['business_id'],
            purchase_quotation_id=self.context['purchase_quotation_id'],
            **self.validated_data
        )

    def update(self, instance, validated_data):
        return super().update(instance, validated_data)

    class Meta:
        model = PurchaseQuotationItem
        fields = ['id', 'product', 'supplier',
                  'quantity', 'unit_price', 'notes']


class PurchaseQuotationSerializer(serializers.ModelSerializer):

    created_by = SimpleUserSerializer(read_only=True)
    items = SimplePurchaseQuotationItemSerializer(many=True)

    class Meta:
        model = PurchaseQuotation
        fields = [
            'id', 'business', 'quotation_no', 'created_by',
            'created_at', 'updated_at', 'notes', 'status',
            'items'
        ]


class PurchaseQuotationCreateSerializer(serializers.ModelSerializer):

    def save(self, **kwargs):
        return PurchaseQuotation.objects.create(
            business_id=self.context['business_id'],
            created_by_id=self.context['user_id'],
            **self.validated_data
        )

    class Meta:
        model = PurchaseQuotation
        fields = ['id', 'quotation_no', 'status', 'notes']


class PurchaseQuotationAndItemsCreateSerializer(serializers.ModelSerializer):
    items = serializers.ListField()
    created_by = SimpleUserSerializer(read_only=True)

    def save(self, **kwargs):
        items = self.validated_data.pop('items')

        with transaction.atomic():
            quotation = PurchaseQuotation.objects.create(
                business_id=self.context['business_id'],
                created_by_id=self.context['user_id'],
                **self.validated_data
            )

            quotation_items = []
            for item in items:
                quotation_items.append(PurchaseQuotationItem(
                    business_id=self.context['business_id'],
                    purchase_quotation=quotation,
                    **item
                ))

            PurchaseQuotationItem.objects.bulk_create(quotation_items)

        return quotation

    class Meta:
        model = PurchaseQuotation
        fields = [
            'id', 'quotation_no', 'created_by',
            'created_at', 'updated_at', 'notes', 'status',
            'items'
        ]


class PurchaseQuotationAndItemsUpdateSerializer(serializers.ModelSerializer):

    items = serializers.ListField()

    def save(self, **kwargs):
        items = self.validated_data.pop('items')

        try:
            with transaction.atomic():
                for attr, value in self.validated_data.items():
                    setattr(self.instance, attr, value)
                self.instance.save()

                existing_items = PurchaseQuotationItem.objects.filter(
                    purchase_quotation_id=self.instance.id
                )
                existing_items_map = {
                    item.id: item for item in existing_items
                }
                existing_item_ids = set(
                    existing_items.values_list('id', flat=True))

                new_items = []
                updated_items = []

                for item in items:
                    # this block essentially does nothing as quotation items
                    # can't be updated on the frontend currently.
                    # attr updation commented as functionality not on frontend yet.

                    if item.get('id', None) in existing_item_ids:
                        quotation_item = existing_items_map.get(item['id'])
                        # invoice_item.product_id = item['product_id']
                        # invoice_item.quantity = item['quantity']
                        # invoice_item.unit_price = item['unit_price']
                        updated_items.append(quotation_item)
                    else:
                        new_items.append(PurchaseQuotationItem(
                            business_id=self.context['business_id'],
                            purchase_quotation=self.instance,
                            product_id=item['product_id'],
                            supplier_id=item['supplier_id'],
                            quantity=item['quantity'],
                            unit_price=item['unit_price']
                        ))

                if new_items:
                    new_items = PurchaseQuotationItem.objects.bulk_create(
                        new_items)

                # Items on the frontend can't be updated,
                # They can either be created or deleted.

                # if updated_items:
                #     SalesInvoiceItem.objects.bulk_update(updated_items, [
                #         'product', 'quantity', 'unit_price'
                #     ])

                updated_ids = [item.id for item in updated_items]
                new_ids = [item.id for item in new_items]
                existing_items = existing_items.exclude(
                    id__in=updated_ids+new_ids)
                existing_items.delete()

                self.instance.refresh_from_db()
                return self.instance

        except Exception as error:
            print(error)
            return None

    class Meta:
        model = PurchaseQuotation
        fields = [
            'id', 'quotation_no', 'notes', 'status', 'items'
        ]
