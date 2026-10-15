import math

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
from .models import InvoiceImage, PurchaseInvoice, PurchaseInvoiceItem, PurchaseQuotation, PurchaseQuotationItem, PurchaseReceipt, SalesInvoice, SalesInvoiceItem, ReturnedItem, SalesReceipt
from .utils import (
    apply_money_details,
    checkPurchaseInvoiceItemFields,
    checkPurchaseInvoiceCreateFields,
    checkSalesInvoiceItemCreateFields,
    invoice_room,
    updateInventoryOnSale
)


MAX_EXTRA_CHARGES = 20
MAX_EXTRA_CHARGE_LABEL = 64


class ExtraChargesValidationMixin:
    """
    Validates the `extra_charges` list a sales invoice carries.

    The column is a JSONField, which accepts any shape at all, and
    SalesInvoice.extra_charges_sum() is deliberately forgiving because it runs
    from a signal where raising would fail an already-accepted write. So this
    is the only place a bad charge can be refused while there is still someone
    to tell about it.

    What it refuses, and why each one matters:

    - an unknown key. A typo'd "ammount" would otherwise print as a charge on
      the invoice and count as zero in the total, which is the worst possible
      outcome: a figure that is wrong and looks deliberate.
    - a negative amount. That is a discount, and because charges are subtracted
      from revenue a negative one would add to it - money appearing in the
      sales figures that nobody was billed.
    - a blank label, a very long one (the PDF column is narrow), or a
      non-finite amount, which no arithmetic survives.

    Duplicate labels are allowed: two separate deliveries on one invoice is an
    ordinary thing. Zero-amount rows are dropped rather than refused, since an
    empty row left behind on the form is a slip, not an error.
    """

    def validate_extra_charges(self, value):
        if value in (None, ''):
            return []

        if not isinstance(value, list):
            raise serializers.ValidationError(
                'Extra charges must be a list of {"label": ..., "amount": ...} '
                'objects.'
            )

        if len(value) > MAX_EXTRA_CHARGES:
            raise serializers.ValidationError(
                f'An invoice can carry at most {MAX_EXTRA_CHARGES} extra '
                f'charges.'
            )

        cleaned = []
        for position, charge in enumerate(value, start=1):
            if not isinstance(charge, dict):
                raise serializers.ValidationError(
                    f'Extra charge {position} must be an object with a label '
                    f'and an amount.'
                )

            unknown = sorted(set(charge) - {'label', 'amount'})
            if unknown:
                raise serializers.ValidationError(
                    f'Extra charge {position} has unexpected '
                    f'{"keys" if len(unknown) > 1 else "key"} '
                    f'{", ".join(unknown)}. Only label and amount are allowed.'
                )

            label = str(charge.get('label') or '').strip()
            if not label:
                raise serializers.ValidationError(
                    f'Extra charge {position} needs a label.'
                )
            if len(label) > MAX_EXTRA_CHARGE_LABEL:
                raise serializers.ValidationError(
                    f'The label on extra charge {position} is longer than '
                    f'{MAX_EXTRA_CHARGE_LABEL} characters.'
                )

            try:
                amount = float(charge.get('amount'))
            except (TypeError, ValueError):
                raise serializers.ValidationError(
                    f'The amount on extra charge {position} is not a number.'
                )
            if not math.isfinite(amount):
                raise serializers.ValidationError(
                    f'The amount on extra charge {position} is not a finite '
                    f'number.'
                )
            if amount < 0:
                raise serializers.ValidationError(
                    f'The amount on extra charge {position} cannot be '
                    f'negative. Use the discount field instead.'
                )

            amount = round(amount, 2)
            if amount == 0:
                continue

            cleaned.append({'label': label, 'amount': amount})

        return cleaned


class InvoiceImageSerializer(serializers.ModelSerializer):
    """
    One photo on an invoice, for reading and for uploading.

    `image` is left as the ModelSerializer's own ImageField rather than a
    method field, so DRF builds an absolute URL from the request in the
    context - which is why the viewset puts it there. A presigned S3 link is
    already absolute and comes through unchanged.

    Which invoice it belongs to comes from the URL, not the payload: these are
    nested routes under an invoice the viewset has already scoped to the
    active business, so accepting an invoice id in the body would be a way to
    attach a photo to someone else's invoice.
    """

    uploaded_by = SimpleUserSerializer(read_only=True)

    class Meta:
        model = InvoiceImage
        fields = ['id', 'image', 'caption', 'uploaded_by', 'uploaded_at']
        read_only_fields = ['id', 'uploaded_by', 'uploaded_at']


class SimpleInvoiceImageSerializer(serializers.ModelSerializer):
    """The same photo, as it is listed inline on an invoice."""

    class Meta:
        model = InvoiceImage
        fields = ['id', 'image', 'caption', 'uploaded_at']


class PaymentReceiptSerializer(serializers.Serializer):

    id = serializers.IntegerField(read_only=True)
    amount = serializers.FloatField()
    desc = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    account = serializers.SerializerMethodField()
    payment_method = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    date = serializers.SerializerMethodField()
    image = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)

    def get_date(self, receipt):
        """
        The day the money moved, which is what the payments dialog shows.

        created_at is the day the row was typed, and backdating is
        unrestricted, so the two differ whenever a payment is entered late.
        The date itself lives on the mirrored transaction, like the account
        and the method below.

        Imported inside the method on purpose: accounts imports sales at load
        time, so a module-level import here would be a cycle. Same convention
        as SalesInvoice.pool_applied.
        """
        from accounts.utils import receipt_date
        return receipt_date(receipt)

    def get_image(self, receipt):
        """
        The photo of the slip, cheque or receipt, on the mirrored transaction
        like everything else about the money.

        Absolute, because the page that shows it is served from another
        origin. A presigned S3 URL is already absolute and comes back
        unchanged; a local one is a bare /media/... path that only works once
        the host is on the front. Without `request` in the serializer context
        there is no host to add, which is why both receipt viewsets put it
        there - the same omission broke every transaction image once.
        """
        transaction = getattr(receipt, 'transaction_record', None)
        image = getattr(transaction, 'image', None) if transaction else None
        if not image:
            return None

        request = self.context.get('request')
        return request.build_absolute_uri(image.url) if request else image.url

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
    # Declared writable here, where the read serializer has it as a method
    # field: a payment carries at most one photo, and it is stored on the
    # transaction the receipt is mirrored into, so this needs no new column.
    image = serializers.ImageField(required=False, allow_null=True)

    MONEY_FIELDS = [
        'account', 'payment_method', 'date', 'cheque_number',
        'cheque_due_date', 'image'
    ]

    def pop_money_details(self):
        details = {}
        for field in self.MONEY_FIELDS:
            value = self.validated_data.pop(field, None)
            if value:
                details['account_id' if field == 'account' else field] = value
        return details

    def apply_money_details(self, receipt, money_details):
        # Delegates to the shared helper so the transaction dispatcher in
        # accounts/ patches a mirrored transaction exactly the way this does.
        apply_money_details(receipt, money_details)
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
    images = SimpleInvoiceImageSerializer(many=True, read_only=True)

    class Meta:
        model = PurchaseInvoice
        fields = [
            'id', 'invoice_number', 'business', 'supplier',
            'date_issued', 'created_at', 'updated_at', 'date_due', 'status', 'payment_status',
            'sub_total', 'tax',  'amount_paid', 'total', 'delivery', 'created_by',
            'notes', 'invoice_items', 'is_restocked', 'is_partially_restocked',
            'projects', 'payment_receipts', 'images'
        ]


class SimplePurchaseInvoiceSerializer(serializers.ModelSerializer):

    supplier = SimpleSupplierSerializer(read_only=True, required=False)
    total_items = serializers.SerializerMethodField()
    amount_paid = serializers.SerializerMethodField()
    projects = ProjectPurchaseInvoiceLinker(many=True)

    def get_total_items(self, obj):
        """
        How many items are on the invoice - the quantities, not the rows.

        .count() answered a different question: it counted distinct product
        variants, so a purchase of twelve of one product reported 1.

        Plain quantity, with no net_quantity equivalent to apply: returns live
        on sales lines only, so a purchase line has nothing deducted from it.

        Summed in Python deliberately: the view prefetches invoice_items, so
        this touches the database not at all, while .count() ignored that
        prefetch and issued a COUNT per row of the list.
        """
        if type(obj) == PurchaseInvoice:
            items = obj.invoice_items.all()
        elif type(obj) == ProjectPurchaseInvoice:
            items = obj.purchase_invoice.invoice_items.all()
        else:
            return None

        return sum(item.quantity for item in items)

    def get_amount_paid(self, obj):
        # transformPurchaseInvoice in the client already reads amount_paid;
        # this serializer had never sent it.
        if type(obj) == ProjectPurchaseInvoice:
            return obj.purchase_invoice.amount_paid
        return obj.amount_paid

    class Meta:
        model = PurchaseInvoice
        fields = [
            'id', 'invoice_number', 'supplier',
            'date_issued', 'created_at', 'date_due', 'status',
            'payment_status', 'sub_total', 'tax', 'total',
            'amount_paid', 'delivery', 'total_items', 'projects'
        ]


class PurchaseInvoiceCreateSerializer(serializers.ModelSerializer):

    business = SimpleBusinessSerializer(read_only=True)

    class Meta:
        model = PurchaseInvoice
        fields = [
            'id', 'invoice_number', 'business', 'supplier',
            'date_issued', 'created_at', 'updated_at', 'date_due', 'status',
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
            'id', 'invoice_number', 'supplier', 'date_issued', 'date_due',
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
            # Re-raised, not swallowed: returning None here turned a
            # failed write into a misleading 400 at the view.
            print(error)
            raise

    class Meta:
        model = PurchaseInvoice
        fields = [
            'invoice_number',
            'supplier',
            'date_issued',
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
            # Re-raised, not swallowed: returning None here turned a
            # failed write into a misleading 400 at the view.
            print(error)
            raise

    class Meta:
        model = PurchaseInvoice
        fields = [
            'invoice_number',
            'supplier',
            'date_issued',
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
        # Let the field validation run first. The old version read
        # self.initial_data["amount"] before this, so a missing amount was a
        # KeyError and a non-numeric one a ValueError - both 500s where a 400
        # belonged. It also raised regardless of raise_exception, breaking
        # DRF's contract.
        valid = super().is_valid(raise_exception=raise_exception)
        if not valid:
            return valid

        purchase_invoice = PurchaseInvoice.objects.get(
            id=self.context["purchase_invoice_id"]
        )
        # A pending cheque still occupies its share of the invoice; a bounced
        # one does not, so its amount can be paid again.
        room = invoice_room(purchase_invoice)

        if float(self.validated_data.get("amount") or 0) > room:
            error = serializers.ValidationError({
                "amount": f"This invoice can take at most PKR {room:,.0f} more - part of it may already be settled from the khaata."
            })
            if raise_exception:
                raise error
            self._errors.update(error.detail)
            return False

        return True

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
            'track_code', 'notes', 'quantity_received', 'is_deducted', 'is_returned'
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
            'quantity_received', 'discount', 'is_deducted', 'is_returned'
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
    images = SimpleInvoiceImageSerializer(many=True, read_only=True)
    # The column holds Draft, Completed or Cancelled; Overdue is worked out
    # here, because whether an invoice is overdue depends on what the khaata
    # has settled onto it and on today's date, neither of which writes to the
    # invoice. display_status is the one definition of the rule.
    status = serializers.SerializerMethodField()

    def get_status(self, obj):
        return obj.display_status

    class Meta:
        model = SalesInvoice
        fields = [
            'id', 'invoice_number', 'business', 'customer', 'date_issued', 'date_due', 'payment_status',
            'status', 'sub_total', 'tax', 'discount', 'extra_charges',
            'extra_charges_total', 'total', 'amount_paid' ,'created_by', 'created_at',
            'notes', 'is_deducted', 'invoice_items', 'projects',
            'payment_receipts', 'images'
        ]
        # Derived from extra_charges by adjust_totals. A client-sent value is
        # ignored rather than trusted - it is the figure every revenue and
        # profit aggregate subtracts.
        read_only_fields = ['extra_charges_total']


class SimpleSalesInvoiceSerializer(serializers.ModelSerializer):

    customer = SimpleCustomerSerializer(read_only=True)
    total_items = serializers.SerializerMethodField()
    amount_paid = serializers.SerializerMethodField()
    projects = ProjectSalesInvoiceLinker(many=True)
    status = serializers.SerializerMethodField()

    def get_total_items(self, obj):
        """
        How many items are on the invoice - the quantities, not the rows.

        .count() answered a different question: it counted distinct product
        variants, so an invoice for six of one product reported 1. net_quantity
        rather than quantity so a returned unit stops counting, which is how
        sub_total is worked out too - otherwise a fully returned line would
        still be listed as sold.

        Summed in Python deliberately: the view prefetches invoice_items, so
        this touches the database not at all, while .count() ignored that
        prefetch and issued a COUNT per row of the list.
        """
        if type(obj) == SalesInvoice:
            items = obj.invoice_items.all()
        elif type(obj) == ProjectSalesInvoice:
            items = obj.sales_invoice.invoice_items.all()
        else:
            return None

        return sum(item.net_quantity for item in items)

    def get_amount_paid(self, obj):
        # payment_status was already here but the figure behind it was not, so
        # a list row could say PARTIALLY_PAID without saying how much. The view
        # prefetches payment_receipts__transaction_record, so the property's
        # loop costs no extra queries.
        if type(obj) == ProjectSalesInvoice:
            return obj.sales_invoice.amount_paid
        return obj.amount_paid

    def get_status(self, obj):
        # Overdue is derived, not stored - see SalesInvoiceSerializer above.
        # The list view attaches the settlement and prefetches the items, so
        # this costs no extra queries per row.
        invoice = obj.sales_invoice if type(obj) == ProjectSalesInvoice else obj
        return invoice.display_status

    class Meta:
        model = SalesInvoice
        fields = [
            'id', 'invoice_number', 'customer', 'date_issued', 'date_due',
            'payment_status', 'status', 'sub_total', 'tax', 'discount',
            'extra_charges', 'extra_charges_total', 'total',
            'amount_paid', 'total_items', 'projects'
        ]
        read_only_fields = ['extra_charges_total']


class SalesInvoiceCreateSerializer(ExtraChargesValidationMixin,
                                   serializers.ModelSerializer):

    business = SimpleBusinessSerializer(read_only=True)

    class Meta:
        model = SalesInvoice
        fields = [
            'id', 'invoice_number', 'business', 'customer', 'date_issued', 'date_due',
            'payment_status', 'tax', 'discount', 'extra_charges', 'notes'
        ]

    def validate(self, attrs):
        super().validate(attrs)

    def save(self, **kwargs):
        return SalesInvoice.objects.create(
            business_id=self.context['business_id'],
            created_by_id=self.context['user_id'],
            **self.validated_data
        )


class SalesInvoiceUpdateSerializer(ExtraChargesValidationMixin,
                                   serializers.ModelSerializer):

    class Meta:
        model = SalesInvoice
        fields = [
            'id', 'invoice_number', 'customer', 'date_issued', 'date_due',
            'payment_status', 'sub_total', 'tax', 'discount', 'extra_charges',
            'total', 'notes'
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


class SalesInvoiceAndItemsCreateSerializer(ExtraChargesValidationMixin,
                                           serializers.ModelSerializer):

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
            # Status before stock: it is derived from the lines, and
            # updateInventoryOnSale only deducts for a COMPLETED invoice.
            sales_invoice.recalculate_status()
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
            # Re-raised, not swallowed: returning None here turned a
            # failed write into a misleading 400 at the view.
            print(error)
            raise

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
            'extra_charges',
            'payment_status',
            'items',
            'project',
            'amount_paid'
        ]


class SalesInvoiceAndItemsUpdateSerializer(ExtraChargesValidationMixin,
                                           serializers.ModelSerializer):

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
                # Deliberately here and not beside the invoice save above: the
                # lines are only final once the new ones are created and the
                # removed ones deleted, and the status is derived from them.
                self.instance.recalculate_status()
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
            # Re-raised, not swallowed: returning None here turned a
            # failed write into a misleading 400 at the view.
            print(error)
            raise

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
            'extra_charges',
            'payment_status',
            'items',
            'project'
        ]


class SalesReceiptCreateSerializer(PaymentReceiptCreateSerializer):

    def is_valid(self, *, raise_exception=False):
        # See PurchaseReceiptCreateSerializer.is_valid for why field validation
        # now runs first.
        valid = super().is_valid(raise_exception=raise_exception)
        if not valid:
            return valid

        sales_invoice = SalesInvoice.objects.get(
            id=self.context["sales_invoice_id"]
        )
        # A pending cheque still occupies its share of the invoice; a bounced
        # one does not, so its amount can be paid again.
        room = invoice_room(sales_invoice)

        if float(self.validated_data.get("amount") or 0) > room:
            error = serializers.ValidationError({
                "amount": f"This invoice can take at most PKR {room:,.0f} more - part of it may already be settled from the khaata."
            })
            if raise_exception:
                raise error
            self._errors.update(error.detail)
            return False

        return True

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
            'extra_charges', 'extra_charges_total', 'total', 'amount_paid'
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
            # Re-raised, not swallowed: returning None here turned a
            # failed write into a misleading 400 at the view.
            print(error)
            raise

    class Meta:
        model = PurchaseQuotation
        fields = [
            'id', 'quotation_no', 'notes', 'status', 'items'
        ]
