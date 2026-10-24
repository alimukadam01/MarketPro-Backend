"""
/backlog-work/ - the entry person's side: the queue, one page at a time, and
the records entered from it.

The only API a data entry account can reach (allow_backlog_role; everything
else is closed at authentication). Every write is tied to the page the user
has claimed, must fit its type, and answers with ids and the values typed -
never a total from elsewhere, a stock figure or a balance (§7).
"""
from datetime import date

from django.db import transaction
from django.db.models import Case, IntegerField, Q, Value, When
from django.utils import timezone
from rest_framework import exceptions, serializers, status
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.viewsets import GenericViewSet

from audit.helpers import audited_update
from inventory.models import InventoryItem
from root.models import Location, ProductVariant
from root.new_records import (
    create_customer, create_product_with_stock, create_supplier, product_answer, supplier_label,
)
from root.serializers import ProductAndVariantCreateSerializer, ProductAndVariantUpdateSerializer
from root.utils import get_active_business, local_date
from sales.serializers import (
    PaymentReceiptSerializer, PurchaseInvoiceAndItemsCreateSerializer,
    PurchaseInvoiceAndItemsUpdateSerializer, PurchaseReceiptCreateSerializer,
    SalesInvoiceAndItemsCreateSerializer, SalesInvoiceAndItemsUpdateSerializer,
    SalesReceiptCreateSerializer,
)
from . import lookups
from .access import HasActiveBusiness, IsEntryWorker, is_owner
from .glossary import current_version
from .models import BacklogEntry, BacklogNewRecord, BacklogType, GlossaryEntry, GlossarySuggestion
from .outputs import OutputInUse, delete_output, describe, record_output
from .serializers import (
    GlossaryEntrySerializer, GlossarySuggestionCreateSerializer, WorkEntrySerializer,
    WorkQueueItemSerializer,
)

NOT_YOURS = {'detail': 'This page is not one you are working on.'}


class StockLineSerializer(serializers.Serializer):
    """One line of a stock or price list. Only what the page can say."""

    product = serializers.IntegerField()
    location = serializers.IntegerField(required=False, allow_null=True)
    quantity = serializers.IntegerField(required=False, allow_null=True, min_value=0)
    counted_at = serializers.DateField(required=False, allow_null=True)
    unit_price = serializers.FloatField(required=False, allow_null=True, min_value=0)
    unit_cost = serializers.FloatField(required=False, allow_null=True, min_value=0)


# What each list type may set on a stock row.
STOCK_FIELDS = {
    BacklogType.STOCK_LIST: ('location', 'quantity', 'counted_at'),
    BacklogType.PRICE_LIST: ('unit_price', 'unit_cost'),
}


class BacklogWorkViewSet(GenericViewSet):
    allow_backlog_role = True
    permission_classes = [IsAuthenticated, IsEntryWorker]
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    throttle_scope = 'backlog_lookup'

    def get_permissions(self):
        # The type-ahead also serves the create dialogs on the invoice
        # screens, which the shop's employees use: names only, so anyone in
        # the business may read it.
        if self.action == 'lookup':
            return [IsAuthenticated(), HasActiveBusiness()]
        return super().get_permissions()

    def get_throttles(self):
        # Only the type-ahead is rate limited (§7): it is the one call a
        # script could use to page through the shop's names.
        if self.action == 'lookup':
            return [ScopedRateThrottle()]
        return []

    # ── helpers ──────────────────────────────────────────────────────────

    def business(self):
        return get_active_business(self.request)

    def entries(self):
        return BacklogEntry.objects.filter(business_id=self.business().id)

    def my_entry(self, pk, review=False):
        """The page this user is working on, or a 403/404 response.

        With `review`, the owner may also open a page waiting for review:
        they check it on the same screen and correct its records in place.
        Only the record endpoints pass it - claiming, releasing and
        submitting stay with the person doing the entry."""
        entry = (self.entries().filter(pk=pk)
                 .prefetch_related('outputs__sales_invoice__customer', 'outputs__purchase_invoice__supplier',
                                   'outputs__product__unit', 'outputs__inventory_item__product__base',
                                   'outputs__inventory_item__location')
                 .first())
        if not entry:
            raise exceptions.NotFound()
        if review and entry.status == BacklogEntry.ENTERED and is_owner(self.request.user, self.business()):
            return entry
        if entry.status != BacklogEntry.IN_PROGRESS or entry.claimed_by_id != self.request.user.id:
            raise exceptions.PermissionDenied(NOT_YOURS['detail'])
        return entry

    def require_type(self, entry, *types):
        if entry.type not in types:
            raise exceptions.ValidationError(
                {'detail': f'This page is a {entry.get_type_display().lower()}.'})

    def my_output(self, entry, output_id):
        output = entry.outputs.filter(pk=output_id).first()
        if not output:
            raise exceptions.NotFound()
        return output

    def entry_data(self, entry):
        return WorkEntrySerializer(entry, context={'request': self.request}).data

    # ── the queue ────────────────────────────────────────────────────────

    @action(['GET'], detail=False, url_path='queue', url_name='queue')
    def queue(self, request):
        """The user's own page in progress first, then the pool: pages sent
        back, today's pages, high priority, oldest first."""
        user = request.user
        rows = (self.entries()
                .filter(Q(status=BacklogEntry.PENDING)
                        | Q(status=BacklogEntry.IN_PROGRESS, claimed_by=user))
                .annotate(
                    mine=Case(When(claimed_by=user, status=BacklogEntry.IN_PROGRESS, then=Value(0)),
                              default=Value(1), output_field=IntegerField()),
                    sent_back=Case(When(rejection_count__gt=0, then=Value(0)),
                                   default=Value(1), output_field=IntegerField()),
                    urgency=Case(When(priority='high', then=Value(0)),
                                 default=Value(1), output_field=IntegerField()))
                .order_by('mine', 'sent_back', '-is_daily', 'urgency', 'created_at', 'id'))
        totals = {
            'waiting': self.entries().filter(status=BacklogEntry.PENDING).count(),
            'daily': self.entries().filter(status=BacklogEntry.PENDING, is_daily=True).count(),
            'entered_today': self.entries().filter(entered_by=user,
                                                   entered_at__date=timezone.localdate()).count(),
            'sent_back': self.entries().filter(status=BacklogEntry.PENDING, rejection_count__gt=0).count(),
        }
        return Response({
            'totals': totals,
            'items': WorkQueueItemSerializer(rows[:200], many=True, context={'request': request}).data,
        })

    def retrieve(self, request, pk=None):
        return Response(self.entry_data(self.my_entry(pk, review=True)))

    @action(['POST'], detail=True, url_path='claim', url_name='claim')
    def claim(self, request, pk=None):
        """Take a page. One conditional update, so two people can never both
        get it: whoever's write lands second changes nothing and gets 409."""
        user = request.user
        if self.entries().filter(status=BacklogEntry.IN_PROGRESS, claimed_by=user).exclude(pk=pk).exists():
            return Response({'detail': 'Finish or release the page you have open first.'},
                            status=status.HTTP_409_CONFLICT)
        taken = audited_update(
            self.entries().filter(pk=pk, status=BacklogEntry.PENDING),
            status=BacklogEntry.IN_PROGRESS, claimed_by=user, claimed_at=timezone.now())
        if not taken:
            return Response({'detail': 'Someone else took this page.'}, status=status.HTTP_409_CONFLICT)
        return Response(self.entry_data(self.my_entry(pk)))

    @action(['POST'], detail=True, url_path='release', url_name='release')
    def release(self, request, pk=None):
        entry = self.my_entry(pk)
        entry.status = BacklogEntry.PENDING
        entry.claimed_by = None
        entry.claimed_at = None
        entry.save(update_fields=['status', 'claimed_by', 'claimed_at', 'updated_at'])
        return Response({'detail': 'OK'})

    @action(['POST'], detail=True, url_path='cant-read', url_name='cant-read')
    def cant_read(self, request, pk=None):
        note = (request.data.get('note') or '').strip()
        if not note:
            return Response({'note': ['Tell the owner what is wrong with the page.']},
                            status=status.HTTP_400_BAD_REQUEST)
        entry = self.my_entry(pk)
        entry.status = BacklogEntry.CANT_READ
        entry.cant_read_note = note
        entry.entered_by = request.user
        entry.entered_at = timezone.now()
        entry.seconds_spent += max(int(request.data.get('seconds_spent') or 0), 0)
        entry.save()
        return Response({'detail': 'OK'})

    @action(['POST'], detail=True, url_path='submit', url_name='submit')
    def submit(self, request, pk=None):
        """Done with the page: it goes to the owner for review. A list page
        is marked complete by submitting it."""
        entry = self.my_entry(pk)
        if not entry.outputs.exists():
            return Response({'detail': 'Enter what is on the page before submitting it.'},
                            status=status.HTTP_400_BAD_REQUEST)
        entry.status = BacklogEntry.ENTERED
        entry.entered_by = request.user
        entry.entered_at = timezone.now()
        entry.seconds_spent += max(int(request.data.get('seconds_spent') or 0), 0)
        entry.glossary_version = current_version(self.business())
        if entry.is_list:
            entry.completed_list = True
        entry.save()
        return Response({'detail': 'OK'})

    # ── what a page produces ─────────────────────────────────────────────

    def _create_invoice(self, request, pk, kind):
        entry = self.my_entry(pk, review=True)
        self.require_type(entry, kind)
        business = self.business()
        serializer_class = (SalesInvoiceAndItemsCreateSerializer if kind == BacklogType.SALES_INVOICE
                            else PurchaseInvoiceAndItemsCreateSerializer)
        serializer = serializer_class(data=request.data, context={
            'business_id': business.id, 'user_id': request.user.id})
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            invoice = serializer.save()
            output = record_output(entry, invoice, request.user)
        return Response({'output': describe(output)}, status=status.HTTP_201_CREATED)

    @action(['POST'], detail=True, url_path='sales-invoice', url_name='sales-invoice')
    def sales_invoice(self, request, pk=None):
        return self._create_invoice(request, pk, BacklogType.SALES_INVOICE)

    @action(['POST'], detail=True, url_path='purchase-invoice', url_name='purchase-invoice')
    def purchase_invoice(self, request, pk=None):
        return self._create_invoice(request, pk, BacklogType.PURCHASE_INVOICE)

    @action(['POST'], detail=True, url_path=r'outputs/(?P<output_id>\d+)/payments',
            url_name='output-payments')
    def payments(self, request, pk=None, output_id=None):
        """A payment written on the bill, against the invoice entered from it."""
        entry = self.my_entry(pk, review=True)
        output = self.my_output(entry, output_id)
        if output.kind == 'sales_invoice':
            serializer = SalesReceiptCreateSerializer(data=request.data, context={
                'request': request, 'sales_invoice_id': output.sales_invoice_id})
        elif output.kind == 'purchase_invoice':
            serializer = PurchaseReceiptCreateSerializer(data=request.data, context={
                'request': request, 'purchase_invoice_id': output.purchase_invoice_id})
        else:
            return Response({'detail': 'Payments go on an invoice.'}, status=status.HTTP_400_BAD_REQUEST)
        serializer.is_valid(raise_exception=True)
        receipt = serializer.save()
        return Response({'id': receipt.id}, status=status.HTTP_201_CREATED)

    @action(['GET'], detail=True, url_path=r'outputs/(?P<output_id>\d+)/settlement',
            url_name='output-settlement')
    def settlement(self, request, pk=None, output_id=None):
        """What the payments dialog needs from the khaata, for this invoice
        only: the figures the standard dialog reads (sales/views.py,
        SettledInvoiceViewMixin.settlement), so the two dialogs agree with
        the server on what is paid and how much one more payment may be.

        Never the party's balance (§7): the credits that settled the
        invoice come back as one line - how much of THIS invoice the khaata
        covers - with no label, date, reference or remaining credit, and
        the overpayment and refund figures are left out."""
        from accounts.settlement import attach_settlement
        from sales.utils import invoice_room

        entry = self.my_entry(pk, review=True)
        output = self.my_output(entry, output_id)
        if output.kind not in ('sales_invoice', 'purchase_invoice'):
            raise exceptions.NotFound()
        invoice = output.record
        attach_settlement([invoice])
        total = float(invoice.total or 0)
        paid = float(invoice.amount_paid or 0)
        from_khaata = round(invoice.pool_applied or 0)
        billed = 0 if invoice.status in (invoice.CANCELLED_STATUSES + invoice.DRAFT_STATUSES) else total
        party = 'customer' if output.kind == 'sales_invoice' else 'supplier'
        return Response({
            'total': round(total),
            'paid_on_invoice': round(invoice.receipts_paid or 0),
            'settled_from_khaata': from_khaata,
            'paid': round(paid),
            'still_owed': round(max(total - paid, 0)),
            'room': round(invoice_room(invoice)),
            'billed': round(billed, 2),
            'settled_by': [{
                'kind': 'khaata', 'id': 0, 'label': f"Already on the {party}'s khaata",
                'reference': '', 'date': None, 'applied': from_khaata,
            }] if from_khaata > 0 else [],
            'refunds': [],
        })

    @action(['DELETE'], detail=True, url_path=r'outputs/(?P<output_id>\d+)/payments/(?P<payment_id>\d+)',
            url_name='output-payment')
    def payment(self, request, pk=None, output_id=None, payment_id=None):
        """Take back a payment entered from the bill."""
        entry = self.my_entry(pk, review=True)
        output = self.my_output(entry, output_id)
        if output.kind not in ('sales_invoice', 'purchase_invoice'):
            raise exceptions.NotFound()
        receipt = output.record.payment_receipts.filter(pk=payment_id).first()
        if not receipt:
            raise exceptions.NotFound()
        receipt.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(['POST'], detail=True, url_path='products', url_name='products')
    def products(self, request, pk=None):
        entry = self.my_entry(pk, review=True)
        self.require_type(entry, BacklogType.PRODUCT_LIST)
        business = self.business()
        serializer = ProductAndVariantCreateSerializer(data=request.data, context={'business_id': business.id})
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            product = serializer.save()
            output = record_output(entry, product, request.user, entered=self._product_values(product))
        return Response({'output': describe(output)}, status=status.HTTP_201_CREATED)

    def _product_values(self, product):
        return {
            'name': product.name, 'unit': product.unit_id, 'desc': product.desc or '',
            'variants': [{'id': v.id, 'attributes': v.attributes or {}} for v in product.variants.all()],
        }

    @action(['POST'], detail=True, url_path='inventory', url_name='inventory')
    def inventory(self, request, pk=None):
        """A stock or price list line: set on the product's stock row, created
        if the shop has none. An existing row's values are kept in the output
        so deleting it puts them back."""
        entry = self.my_entry(pk, review=True)
        self.require_type(entry, BacklogType.STOCK_LIST, BacklogType.PRICE_LIST)
        values = self._stock_values(entry, request.data)
        business = self.business()
        variant = ProductVariant.objects.filter(pk=values.pop('product'), base__business_id=business.id).first()
        if not variant:
            raise exceptions.ValidationError({'product': ['Pick a product from the list.']})
        if entry.outputs.filter(inventory_item__product=variant).exists():
            raise exceptions.ValidationError({'product': ['That product is already on this page.']})

        with transaction.atomic():
            row = InventoryItem.objects.select_for_update().filter(
                business_id=business.id, product=variant).first()
            changes = self._row_changes(values)
            if row:
                previous = {field: getattr(row, field) for field in changes}
                previous = {k: (v.isoformat() if isinstance(v, date) else v) for k, v in previous.items()}
                for field, value in changes.items():
                    setattr(row, field, value)
                row.save()
                created = False
            else:
                row = InventoryItem.objects.create(
                    business_id=business.id, inventory=business.inventory_glance, product=variant,
                    quantity=changes.pop('quantity', 0), quantity_on_hand=changes.pop('quantity_on_hand', 0),
                    **changes)
                previous, created = {}, True
            output = record_output(entry, row, request.user, created=created,
                                   entered=self._entered(values, variant), previous=previous)
        return Response({'output': describe(output)}, status=status.HTTP_201_CREATED)

    def _stock_values(self, entry, data):
        serializer = StockLineSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        allowed = STOCK_FIELDS[entry.type]
        values = {key: value for key, value in serializer.validated_data.items()
                  if key == 'product' or key in allowed}
        if entry.type == BacklogType.STOCK_LIST and values.get('quantity') is None:
            raise exceptions.ValidationError({'quantity': ['Enter the quantity counted.']})
        if entry.type == BacklogType.PRICE_LIST and values.get('unit_price') is None \
                and values.get('unit_cost') is None:
            raise exceptions.ValidationError({'unit_price': ['Enter a price.']})
        location = values.get('location')
        if location and not Location.objects.filter(pk=location, business_id=self.business().id).exists():
            raise exceptions.ValidationError({'location': ['Pick a location from the list.']})
        return values

    def _row_changes(self, values):
        """Stock row fields from a list line. A count sets both the quantity
        and what is on hand, and dates the count (§8)."""
        changes = {}
        if 'location' in values:
            changes['location_id'] = values['location']
        if values.get('quantity') is not None:
            changes['quantity'] = values['quantity']
            changes['quantity_on_hand'] = values['quantity']
            changes['stock_counted_at'] = values.get('counted_at') or timezone.localdate()
        for field in ('unit_price', 'unit_cost'):
            if values.get(field) is not None:
                changes[field] = values[field]
        return changes

    def _entered(self, values, variant):
        entered = {key: (value.isoformat() if isinstance(value, date) else value)
                   for key, value in values.items()}
        entered['product'] = variant.id
        return entered

    @action(['GET', 'PUT', 'DELETE'], detail=True, url_path=r'outputs/(?P<output_id>\d+)',
            url_name='output')
    def output(self, request, pk=None, output_id=None):
        """One record from the page: read it back for editing, change it,
        or delete it (stock reversed - backlog/outputs.py)."""
        entry = self.my_entry(pk, review=True)
        output = self.my_output(entry, output_id)
        if request.method == 'GET':
            return Response({**describe(output), 'values': self._values_of(output)})
        if request.method == 'DELETE':
            try:
                delete_output(output)
            except OutputInUse as error:
                return Response({'detail': str(error)}, status=status.HTTP_409_CONFLICT)
            return Response(status=status.HTTP_204_NO_CONTENT)
        return self._update_output(request, entry, output)

    def _values_of(self, output):
        if output.kind == 'product':
            return self._product_values(output.product)
        if output.kind == 'inventory_item':
            return output.entered
        if output.kind in ('sales_invoice', 'purchase_invoice'):
            return self._invoice_values(output)
        return {}

    def _invoice_values(self, output):
        """Everything the invoice entry form holds, so a page sent back - or
        opened for review - loads as it was entered. Only what is on the
        bill: its lines, its adjustments and the payments written on it."""
        invoice = output.record
        sales = output.kind == 'sales_invoice'
        party = invoice.customer if sales else invoice.supplier
        lines = invoice.invoice_items.select_related('product__base')
        link = invoice.projects.select_related('project').first()
        receipts = invoice.payment_receipts.select_related('transaction_record__account')
        values = {
            'invoice_number': invoice.invoice_number,
            'party': {'id': party.id, 'label': party.name} if party else None,
            # The day in the shop's time zone. date_issued is a timestamp, and
            # read as UTC its first ten characters are the day before for
            # anything saved at local midnight - which every save then wrote
            # back, moving the date a day earlier each time.
            'date_issued': local_date(invoice.date_issued),
            'date_due': invoice.date_due,
            'tax': invoice.tax or {},
            'notes': invoice.notes or '',
            'project': {'id': link.project_id, 'label': link.project.name} if link else None,
            'lines': [{'id': line.id, 'product': line.product_id, 'label': describe_variant(line.product),
                       'quantity': line.quantity - (getattr(line, 'returned_quantity', 0) or 0),
                       'price': line.unit_price if sales else line.unit_cost}
                      for line in lines],
            'payments': PaymentReceiptSerializer(receipts, many=True, context={'request': self.request}).data,
            'total': invoice.total,
        }
        if sales:
            values.update(discount=invoice.discount or {},
                          extra_charges=invoice.extra_charges or [])
        else:
            values.update(delivery=invoice.delivery, status=invoice.status)
        return values

    def _update_output(self, request, entry, output):
        if output.kind == 'product':
            if not output.created:
                return Response({'detail': 'Only a product entered from this page can be changed here.'},
                                status=status.HTTP_400_BAD_REQUEST)
            serializer = ProductAndVariantUpdateSerializer(
                output.product, data=request.data, context={'business_id': self.business().id})
            serializer.is_valid(raise_exception=True)
            product = serializer.save()
            output.entered = self._product_values(product)
            output.save(update_fields=['entered'])
            return Response({'output': describe(output)})
        if output.kind == 'inventory_item':
            values = self._stock_values(entry, {**request.data, 'product': output.inventory_item.product_id})
            values.pop('product')
            row = output.inventory_item
            for field, value in self._row_changes(values).items():
                setattr(row, field, value)
            row.save()
            output.entered = self._entered(values, row.product)
            output.save(update_fields=['entered'])
            return Response({'output': describe(output)})
        # An invoice: the same update the invoice screens use, so its stock
        # and payments move exactly as they would there.
        sales = output.kind == 'sales_invoice'
        serializer_class = SalesInvoiceAndItemsUpdateSerializer if sales else PurchaseInvoiceAndItemsUpdateSerializer
        serializer = serializer_class(output.record, data=request.data, context={
            'business_id': self.business().id, 'user_id': request.user.id})
        serializer.is_valid(raise_exception=True)
        # The update gives a removed sales line's stock back itself
        # (sales/utils.py return_removed_sales_line), as on the invoice screens.
        with transaction.atomic():
            serializer.save()
        output.refresh_from_db()
        return Response({'output': describe(output)})

    # ── new customers, suppliers and products (BUGS.md #006) ─────────────
    #
    # A name on the page that MarketPro does not have yet is created from
    # the entry screen, tied to the page the user has open. The answer is
    # the new record's id and name, as a lookup's is - never a figure.

    @action(['POST'], detail=True, url_path='new-customer', url_name='new-customer')
    def new_customer(self, request, pk=None):
        entry = self.my_entry(pk, review=True)
        with transaction.atomic():
            customer = create_customer(self.business(), request.data)
            BacklogNewRecord.objects.create(entry=entry, customer=customer, created_by=request.user)
        return Response({'id': customer.id, 'label': customer.name}, status=status.HTTP_201_CREATED)

    @action(['POST'], detail=True, url_path='new-supplier', url_name='new-supplier')
    def new_supplier(self, request, pk=None):
        entry = self.my_entry(pk, review=True)
        with transaction.atomic():
            supplier = create_supplier(self.business(), request.data)
            BacklogNewRecord.objects.create(entry=entry, supplier=supplier, created_by=request.user)
        return Response({'id': supplier.id, 'label': supplier_label(supplier)}, status=status.HTTP_201_CREATED)

    @action(['POST'], detail=True, url_path='new-product', url_name='new-product')
    def new_product(self, request, pk=None):
        """A product with its variants and a stock row for each
        (root/new_records.py)."""
        entry = self.my_entry(pk, review=True)
        with transaction.atomic():
            product, variants = create_product_with_stock(self.business(), request.data)
            BacklogNewRecord.objects.create(entry=entry, product=product, created_by=request.user)
        return Response(product_answer(product, variants), status=status.HTTP_201_CREATED)

    # ── lookups and the glossary ─────────────────────────────────────────

    @action(['GET'], detail=False, url_path=r'lookup/(?P<kind>[a-z]+)', url_name='lookup')
    def lookup(self, request, kind=None):
        finder = lookups.KINDS.get(kind)
        if not finder:
            raise exceptions.NotFound()
        return Response(finder(self.business(), request.query_params.get('q', '')))

    @action(['GET'], detail=False, url_path='glossary', url_name='glossary')
    def glossary(self, request):
        business = self.business()
        rows = (GlossaryEntry.objects.filter(business_id=business.id).current()
                .select_related('customer', 'supplier', 'product').order_by('term'))
        return Response({'version': current_version(business),
                         'entries': GlossaryEntrySerializer(rows, many=True).data})

    @action(['POST'], detail=False, url_path='glossary-suggestions', url_name='glossary-suggestions')
    def suggest(self, request):
        business = self.business()
        serializer = GlossarySuggestionCreateSerializer(data=request.data, context={'business_id': business.id})
        serializer.is_valid(raise_exception=True)
        values = serializer.values()
        entry_id = request.data.get('entry')
        entry = self.entries().filter(pk=entry_id).first() if entry_id else None
        suggestion = GlossarySuggestion.objects.create(
            business=business, entry=entry, suggested_by=request.user, **values)
        return Response({'id': suggestion.id}, status=status.HTTP_201_CREATED)


def describe_variant(variant):
    from .outputs import variant_label
    return variant_label(variant) if variant else ''
