from calendar import monthrange
from datetime import datetime
from typing import Dict, List
from django.db import models
from django.db.models import Avg, Sum, F, Q, Value
from django.db.models.functions import Coalesce
from django.core.exceptions import ValidationError
from django.conf import settings
from django.utils import timezone
from root.utils import generateTransactionId, local_date
from root.models import Business, BusinessConfig, Customer, BaseItem, Supplier, Location, BaseQuerySet


# Create your models here.

class SalesConfig(models.Model):
    business_config = models.OneToOneField(
        BusinessConfig, on_delete=models.CASCADE, related_name="sales_config")
    on_sale_auto_update_inventory = models.BooleanField(default=True)
    on_purchase_auto_update_inventory = models.BooleanField(default=True)
    is_returned_items_enabled = models.BooleanField(default=False)


class BaseRestock(models.Model):

    quantity = models.PositiveIntegerField(default=0)
    # auto_now_add would use the server's OS date rather than TIME_ZONE.
    received_at = models.DateField(
        default=timezone.localdate, null=True, blank=True)
    # Optional link to external txn
    notes = models.TextField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        ordering = ['-created_at']


class PaymentReceipt(models.Model):

    amount = models.FloatField(default=0.0)
    desc = models.CharField(max_length=512, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class SettledInvoiceMixin:
    """
    What an invoice has been paid, shared by sales and purchase invoices.

    Two layers, always in this order (credit-debit-notes plan, section 4):

    1. receipts_paid - payments recorded against THIS invoice. Stored; the
       owner chose them.
    2. pool_applied - the share of the party's unnamed money (credit notes,
       on-account payments, overpayments, a credit opening balance) that the
       khaata settles onto this invoice, opening balance first and then
       oldest-first. Computed by accounts.settlement, never stored.

    amount_paid is the sum, so every reader that means "how much of this
    invoice is settled" - status, pending balance, the WhatsApp message -
    agrees with the khaata.
    """

    @property
    def receipts_paid(self):
        # Python-side sum so prefetched receipts are reused on list views.
        # Only money that has actually cleared pays the invoice down — a
        # pending or bounced cheque is recorded against it but settles
        # nothing until it clears. The settlement engine reads THIS, never
        # amount_paid, which would recurse into it.
        total = 0
        for receipt in self.payment_receipts.all():
            transaction = getattr(receipt, 'transaction_record', None)
            if transaction and transaction.status != 'C':
                continue
            total += receipt.amount or 0
        return total

    @property
    def pool_applied(self):
        """
        Cached on the instance: accounts.settlement.attach_settlement sets it
        for a whole page of invoices in one pass per party. A lone invoice
        computes its party's khaata on first use.
        """
        cached = getattr(self, '_pool_applied', None)
        if cached is None:
            # Function-level import: accounts imports this module at load
            # time, so importing accounts back at module level would make a
            # cycle on startup.
            from accounts.settlement import attach_settlement
            attach_settlement([self])
            cached = self._pool_applied
        return cached

    @property
    def amount_paid(self):
        return self.receipts_paid + self.pool_applied

    @property
    def payment_status(self):
        """
        Always derived - from the invoice's own payments plus what the khaata
        settles onto it. Never stored, never set by hand.
        """
        paid = self.amount_paid
        invoice_total = self.total or 0

        if invoice_total > 0 and paid >= invoice_total:
            return 'P'
        if paid > 0:
            return 'PP'
        return 'PEN'


# An invoice total with its extra charges taken back out.
#
# A delivery or labour charge is money collected alongside the sale, not sales
# value, so it is excluded from revenue, from profit and from the dashboard
# sales figure - while the customer is still invoiced for it and still owes it,
# which is why the ledger, the party balance and payment_status all use `total`
# unchanged.
#
# Coalesce guards the aggregate, not the column: extra_charges_total is
# non-null with a default of 0.0, but an expression over a LEFT JOIN or a
# values() on a related model can still hand it NULL, and one NULL would make
# the whole subtraction NULL. `total` itself stays nullable, so an itemless
# invoice drops out of a Sum exactly as it did before.
GOODS_TOTAL = F('total') - Coalesce(F('extra_charges_total'), Value(0.0))


class SalesInvoiceQuerySet(BaseQuerySet):

    pass


class SalesInvoiceManager(models.Manager):

    def get_queryset(self):
        return SalesInvoiceQuerySet(self.model)

    def total_sales(self, business_id, num_days=None):
        queryset = self.get_queryset().for_business(business_id)

        if num_days:
            queryset = queryset.in_period(num_days)

        return queryset.aggregate(total=Sum(GOODS_TOTAL))["total"] or 0

    def monthly_sales_trend(self, business_id):
        # An expression rather than a field name: monthly_trend passes whatever
        # it is given straight to Sum(), which takes either.
        return self.get_queryset().monthly_trend(business_id, GOODS_TOTAL)

    def recent_sales(self, business_id):
        queryset = self.get_queryset().for_business(
            business_id).order_by('-created_at')[:4]

        res = []
        for sale in queryset:
            invoice_items = sale.invoice_items.all()
            for item in invoice_items:
                res.append({
                    "id": sale.id,
                    "product": f"{item.product.base.name} ({item.product.name})",
                    "quantity": item.quantity,
                    "price": item.unit_price
                })

        return res[:6]

    def average_order_value(self, business_id):
        # `total` is nullable and stays NULL until adjust_totals runs, which only
        # happens when an invoice has line items - so a saved invoice with none
        # crashed the Python-level sum this used to do. SQL AVG is also the right
        # semantic, not just the safe one: it drops NULL rows from the numerator
        # AND the denominator, so an itemless shell no longer counts as a zero
        # value order and drags the average down, while an invoice that really
        # totals 0.0 still counts. Returns 0 for a business with no invoices,
        # matching total_sales above.
        #
        # Charges are deliberately left in, unlike total_sales and revenue:
        # this is the average bill a customer was handed, and the delivery fee
        # was on it.
        queryset = self.get_queryset().for_business(business_id)

        return int(queryset.aggregate(avg=Avg("total"))["avg"] or 0)

    ### The two below are the absolute-window siblings of total_sales and
    ### total_invoices, written for the targets module. They differ from those
    ### deliberately: an explicit date range instead of a relative one, the
    ### invoice date instead of created_at, cancelled invoices excluded, and
    ### optional filters. The originals are left as they are because existing
    ### dashboards depend on them.

    def revenue(self, business_id, date_from, date_to,
                customer_id=None, created_by_id=None, status=None):
        """
        Invoiced sales value over an absolute window, dated on the invoice.

        Cancelled invoices never count. Drafts do, which matches what
        party_invoiced, profit_estimate and the Sales page already show.

        Net of extra charges - see GOODS_TOTAL. A sales target is a target for
        selling things, and a delivery fee is not a thing sold.
        """
        return (
            self.get_queryset()
            .for_business(business_id)
            .exclude(status__in=self.model.CANCELLED_STATUSES)
            .in_range('date_issued__date', date_from, date_to)
            .filter_optional(
                customer_id=customer_id,
                created_by_id=created_by_id,
                status=status,
            )
            .aggregate(value=Sum(GOODS_TOTAL))['value'] or 0
        )

    def invoice_count(self, business_id, date_from, date_to,
                      customer_id=None, created_by_id=None, status=None):
        """How many invoices revenue() would have summed."""
        return (
            self.get_queryset()
            .for_business(business_id)
            .exclude(status__in=self.model.CANCELLED_STATUSES)
            .in_range('date_issued__date', date_from, date_to)
            .filter_optional(
                customer_id=customer_id,
                created_by_id=created_by_id,
                status=status,
            )
            .count()
        )


class SalesInvoice(SettledInvoiceMixin, models.Model):

    # Status is derived, not chosen. An invoice is a Draft until it carries a
    # line, Completed once it does, and Overdue while it is unpaid past its due
    # date. Cancelling is the one transition a person makes, and it sticks.
    #
    #   ("S",  "SENT")                 removed - no client used it
    #   ("PC", "PARTIALLY_COMPLETED")  removed - it was never a document state,
    #                                  only a copy of how much stock had moved,
    #                                  and writing it over the user's choice is
    #                                  what made an edited invoice unfixable.
    SALES_INVOICE_STATUS_CHOICES = [
        ("D", "DRAFT"),
        ("O", "OVERDUE"),
        ("X", "CANCELLED"),
        ("C", "COMPLETED"),
    ]

    # Which states mean the sale never happened. Kept here rather than imported
    # from accounts, which imports this module and cannot be imported back.
    # Note 'C' means COMPLETED here and CANCELLED on a purchase invoice.
    CANCELLED_STATUSES = ["X"]
    # Not confirmed yet, so not a debt: left out of the khaata with cancelled.
    DRAFT_STATUSES = ["D"]
    # Set by a person and never recalculated away.
    MANUAL_STATUSES = ["X"]
    # Worked out on every read and never stored: whether an invoice is overdue
    # depends on what it has been paid, and the khaata settles that on read
    # (accounts/settlement.py stores nothing). It also changes when the date
    # rolls over, with no write to the invoice at all, so a stored value would
    # be wrong by construction and there is no scheduler here to refresh it.
    DERIVED_ONLY_STATUSES = ["O"]

    PAYMENT_STATUS_CHOICES = [
        ("P", "PAID"),
        ("PP", "PARTIALLY_PAID"),
        ("PEN", "PENDING"),
        ("RF", "REFUNDED"),
        ("C", "CANCELLED"),
    ]

    invoice_number = models.CharField(max_length=256, null=True, blank=True)
    business = models.ForeignKey(Business, models.CASCADE)
    customer = models.ForeignKey(
        Customer, models.CASCADE, related_name='sale_invoices')
    # Editable, so a sale entered a day late can carry the day it happened.
    date_issued = models.DateTimeField(default=timezone.now)
    date_due = models.DateField(null=True, blank=True)
    # 'D' as a plain string. This read SALES_INVOICE_STATUS_CHOICES[2], which
    # is the whole ("O", "OVERDUE") tuple rather than a code - and once SENT
    # and PARTIALLY_COMPLETED were removed, index 2 became CANCELLED, which
    # would have quietly dropped every defaulted invoice out of the khaata.
    status = models.CharField(
        max_length=256, choices=SALES_INVOICE_STATUS_CHOICES, default='D')
    sub_total = models.FloatField(null=True, blank=True)
    tax = models.JSONField(
        null=True,
        blank=True,
        default=dict,
        help_text='e.g., {"value": 10.0, "type": "percentage" or "amount"}'
    )
    discount = models.JSONField(
        null=True,
        blank=True,
        default=dict,
        help_text='e.g., {"value": 10.0, "type": "percentage" or "amount"}'
    )
    # Named charges added after tax - delivery, labour, a cutting fee. A list
    # rather than a column per charge, because which ones a shop adds is its
    # own business and they differ per invoice.
    extra_charges = models.JSONField(
        null=True,
        blank=True,
        default=list,
        help_text='e.g., [{"label": "Delivery", "amount": 500.0}]'
    )
    # Denormalised from extra_charges by adjust_totals, and written nowhere
    # else. It is what keeps "revenue excluding charges" a plain subtraction in
    # SQL: summing inside a JSON column is not portable between the SQLite used
    # in development and the PostgreSQL used in production. sub_total is
    # already exactly this kind of stored derivation.
    extra_charges_total = models.FloatField(default=0.0)
    total = models.FloatField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, models.CASCADE, 'created_sales_invoices'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(null=True, blank=True)
    is_deducted = models.BooleanField(default=False)

    objects = SalesInvoiceManager()

    def update_deduction_flags(self):
        """
        Whether every line has left stock. A stock fact only - it no longer
        touches `status`, which is what made an invoice stick on PARTIALLY
        COMPLETED for ever once a line was added during an update.
        """
        items = self.invoice_items.all()
        self.is_deducted = bool(items) and all(
            item.is_deducted for item in items)

    @property
    def amount_due(self):
        """
        What is still owed: the total less its own receipts AND whatever the
        party's khaata settles onto it. Never negative-by-rounding.
        """
        return round(float(self.total or 0) - float(self.amount_paid or 0), 2)

    @property
    def is_overdue(self):
        # The same epsilon accounts/settlement.py uses, so a rounding tail
        # cannot make a settled invoice look overdue.
        return bool(
            self.date_due
            and self.date_due < timezone.localdate()
            and self.amount_due > 0.005
        )

    @property
    def display_status(self):
        """
        The status a person should see. The single definition of the rule:
        the serializers, the list filter and recalculate_status all read it,
        so they cannot drift apart.
        """
        if self.status in self.MANUAL_STATUSES:
            return self.status
        # len() rather than .exists(): the list view prefetches invoice_items,
        # and len() reuses that cache while .exists() costs a query per row.
        if not len(self.invoice_items.all()):
            return 'D'
        if self.is_overdue:
            return 'O'
        return 'C'

    @property
    def stored_status(self):
        """display_status with Overdue folded back into Completed."""
        derived = self.display_status
        return 'C' if derived in self.DERIVED_ONLY_STATUSES else derived

    def recalculate_status(self):
        """
        Bring the stored status back in line with the invoice's contents.

        Written with a queryset update rather than self.save(): that fires no
        signals, so a status write can never re-enter the item/invoice save
        cycle that adjust_totals already runs inside. The in-memory value is
        set too, so the response serialises the new status without a refetch.
        """
        new_status = self.stored_status
        if new_status == self.status:
            return False
        self.status = new_status
        SalesInvoice.objects.filter(pk=self.pk).update(status=new_status)
        return True

    def __str__(self):
        return f"{self.id}-{self.invoice_number}-{self.total}-{self.status}"

    def extra_charges_sum(self):
        """
        This invoice's named charges, totalled.

        Tolerant of a malformed row rather than raising: adjust_totals runs
        from a signal on every item save, and an exception there would fail a
        write the serializer has already accepted. The serializer is what
        rejects a bad charge, at the point a person can still be told about it.
        """
        total = 0.0
        for charge in (self.extra_charges or []):
            try:
                total += float(charge.get('amount') or 0)
            except (AttributeError, TypeError, ValueError):
                continue
        return round(total, 2)

    def adjust_totals(self):
        items = self.invoice_items.exclude(returned_quantity = F("quantity"))
        if not items:
            # An invoice with no lines keeps a NULL total, which
            # average_order_value relies on to drop it from the average rather
            # than counting it as an order worth nothing. Its charges are left
            # on the record but are not a total on their own: a delivery fee
            # with nothing being delivered is an invoice still being written.
            return

        subtotal = sum(item.net_quantity * item.unit_price for item in items)

        # Calculate total discount on the order.
        discount = 0
        if self.discount:
            if self.discount["type"] == "percentage":
                discount += self.discount["value"]/100 * subtotal

            elif self.discount["type"] == "amount":
                discount += self.discount["value"]

            else:
                raise Exception("incorrect value")

        else:
            for item in items:
                if not item.discount:
                    continue

                if item.discount["type"] == "percentage":
                    discount += item.unit_price * (item.discount["value"]/100)

                elif item.tax["type"] == "amount":
                    discount += item.discount["value"]

                else:
                    raise Exception("incorrect value")

        # Calculate tax on the Invoice
        tax = 0
        if self.tax:
            if self.tax['type'] == "percentage":
                tax = subtotal * (self.tax['value']/100)

            elif self.tax['type'] == "amount":
                tax += self.tax['value']

            else:
                raise Exception("incorrect value")

        # Charges are added after tax and are not part of sub_total.
        #
        # Both matter. Invoice.jsx recomputes a percentage tax and discount
        # from sub_total to print them, so folding the charges in would have it
        # charge tax on the delivery fee; and adding them before tax would
        # charge real tax on it here. A charge is a pass-through, so it is
        # taxed by neither.
        charges = self.extra_charges_sum()

        self.sub_total = subtotal
        self.extra_charges_total = charges
        self.total = subtotal + tax - discount + charges
        self.save(update_fields=["sub_total", "total", "extra_charges_total"])

    # is_fulfilled() stood here and was dead: nothing called it, and it
    # assigned self.is_restocked / self.is_partially_restocked, which are
    # PurchaseInvoice fields that SalesInvoice does not have - so it would
    # have raised had anything ever reached it. It also set status = 'C',
    # which is now derived. Removed with the rest of the status overload.

    def items_by_product(self) -> Dict[int, List['SalesInvoiceItem']]:
        """
        This invoice's lines grouped by product - a LIST per product, because
        one product may now appear on several lines at different prices.

        It used to be {product_id: item}, which silently kept only the last
        line, so stock for every earlier line of the same product was never
        deducted. The purchase side keeps its one-item map: its lines are
        still unique per product.
        """
        grouped: Dict[int, List['SalesInvoiceItem']] = {}
        for item in self.invoice_items.all():
            grouped.setdefault(item.product_id, []).append(item)
        return grouped


class SalesInvoiceItemQuerySet(BaseQuerySet):
    pass


class SalesInvoiceItemManager(models.Manager):

    def get_queryset(self):
        return SalesInvoiceItemQuerySet(self.model)
    
    def total_items_sold(self, business_id, num_days):
        queryset = self.get_queryset().for_business(business_id).in_period(num_days)
        total_items = 0

        for item in queryset:
            total_items += item.quantity

        return total_items

    def net_items_sold(self, business_id, date_from, date_to,
                       customer_id=None, product_id=None,
                       product_variant_id=None, created_by_id=None):
        """
        Quantity sold less quantity returned, over an absolute window.

        The subtraction is done in SQL with F(): net_quantity is a Python
        property and cannot be used in an aggregate. A fully returned line
        nets to zero on its own, so do not reach for adjust_totals's
        .exclude(returned_quantity=F('quantity')) — that drops whole rows,
        which is wrong when summing a difference.

        Tenancy goes through the invoice rather than the item's own
        business_id: the invoice is the authority, and it is the path
        profit_estimate already uses.

        product_id filters the Product while product_variant_id filters the
        variant, because BaseItem.product points at ProductVariant. It reads
        backwards; it is not a mistake.
        """
        return (
            self.get_queryset()
            .filter(sales_invoice__business_id=business_id)
            .exclude(sales_invoice__status__in=SalesInvoice.CANCELLED_STATUSES)
            .in_range('sales_invoice__date_issued__date', date_from, date_to)
            .filter_optional(
                sales_invoice__customer_id=customer_id,
                product__base_id=product_id,
                product_id=product_variant_id,
                sales_invoice__created_by_id=created_by_id,
            )
            .aggregate(value=Sum(F('quantity') - F('returned_quantity')))['value'] or 0
        )


class SalesInvoiceItem(BaseItem):
    sales_invoice = models.ForeignKey(
        SalesInvoice, models.CASCADE, related_name='invoice_items'
    )
    unit_price = models.FloatField(null=True, blank=True)
    discount = models.JSONField(
        null=True,
        blank=True,
        default=dict,
        help_text='e.g., {"value": 10.0, "type": "percentage" or "amount"}'
    )
    quantity_received = models.IntegerField(default=0)
    returned_quantity = models.PositiveIntegerField(default=0)
    is_deducted = models.BooleanField(default=False)

    objects = SalesInvoiceItemManager()

    @property
    def net_quantity(self):
        return self.quantity - self.returned_quantity

    @property
    def is_returned(self):
        return self.quantity == self.returned_quantity
    
    @property
    def is_partially_returned(self):
        return self.returned_quantity > 0 and not self.is_returned

    def compute_restock_delta(self) -> int:
        """
        Sum up all past restocks to find out how many units remain to add.
        """
        agg = self.restocks.aggregate(total=models.Sum('quantity'))
        prev = agg.get('total') or 0
        return getattr(self, 'quantity') - prev

    def update_restock_flags(self):
        """
        Whether this line has fully left stock.

        Read from the deduction ledger, not from quantity_received. The two
        disagree: compute_restock_delta() above measures what is left to
        deduct as quantity minus the logged deductions, while this used to
        compare quantity_received - a field the sales update path never set.
        So a line could be fully deducted in the ledger and still be flagged
        partial for ever, which is exactly what pinned an edited invoice to
        PARTIALLY COMPLETED.

        Only ever called right after a deduction is logged, so the ledger is
        current when it runs.
        """
        deducted = self.restocks.aggregate(
            total=models.Sum('quantity'))['total'] or 0
        self.is_deducted = deducted >= self.quantity
        self.save(update_fields=['is_deducted'])
    
    def __str__(self):
        return f'{self.id}_{self.product.base.name} ({self.product.name})_{self.sales_invoice.id}_{self.sales_invoice.invoice_number}'
    
    class Meta:
        # unit_price is part of the key: a shop bills the same product at
        # different prices on one invoice - four paint colours off one base
        # product, an old price beside a new one - and that was a 500 while
        # the key was (invoice, product) alone. Billing it twice at the SAME
        # price is still refused, because that is a double-entered line: the
        # invoice screen merges it into the existing row's quantity instead.
        unique_together = [('sales_invoice', 'product', 'unit_price')]


class SalesReceipt(PaymentReceipt):

    sales_invoice = models.ForeignKey(SalesInvoice, on_delete=models.CASCADE, related_name='payment_receipts')


class PurchaseInvoiceQuerySet(BaseQuerySet):
    pass


class PurchaseInvoiceManager(models.Manager):

    def get_queryset(self):
        return PurchaseInvoiceQuerySet(self.model)

    def total_purchases(self, business_id, num_days=None):
        queryset = self.get_queryset().for_business(business_id)

        if num_days:
            queryset = queryset.in_period(num_days)

        return queryset.aggregate(total=Sum("total"))["total"] or 0

    def purchase_value(self, business_id, date_from, date_to,
                       supplier_id=None, created_by_id=None, status=None):
        """
        Invoiced purchase value over an absolute window, dated on the invoice.

        Gross of any negotiated discount: PurchaseInvoice has no discount
        field, so a discount only shows here if the user lowered unit_cost by
        hand. The catalogue label says so; nothing in the UI may call this
        figure net.
        """
        return (
            self.get_queryset()
            .for_business(business_id)
            .exclude(status__in=self.model.CANCELLED_STATUSES)
            .in_range('date_issued__date', date_from, date_to)
            .filter_optional(
                supplier_id=supplier_id,
                created_by_id=created_by_id,
                status=status,
            )
            .aggregate(value=Sum('total'))['value'] or 0
        )

    def purchase_invoice_count(self, business_id, date_from, date_to,
                               supplier_id=None, created_by_id=None, status=None):
        """How many invoices purchase_value() would have summed."""
        return (
            self.get_queryset()
            .for_business(business_id)
            .exclude(status__in=self.model.CANCELLED_STATUSES)
            .in_range('date_issued__date', date_from, date_to)
            .filter_optional(
                supplier_id=supplier_id,
                created_by_id=created_by_id,
                status=status,
            )
            .count()
        )

    def total_pending_invoices(self, business_id):
        """
        Invoices not yet paid in full - by their own payments AND what the
        supplier's khaata settles onto them, the same payment_status the
        purchases table shows. Drafts and cancelled invoices are not debts,
        so they are not pending either.

        This used to count every invoice whose own receipts fell short,
        cancelled ones included, so an invoice the khaata had settled stayed
        "pending" here for ever.
        """
        # Function-level import: accounts imports this module at load time.
        from accounts.settlement import attach_settlement

        model = self.model
        invoices = list(
            self.get_queryset()
            .for_business(business_id)
            .exclude(status__in=model.CANCELLED_STATUSES + model.DRAFT_STATUSES)
            .prefetch_related('payment_receipts__transaction_record')
        )
        attach_settlement(invoices)
        return sum(1 for invoice in invoices if invoice.payment_status != 'P')


class PurchaseInvoice(SettledInvoiceMixin, models.Model):

    PURCHASE_INVOICE_STATUS_CHOICES = [
        ("D", "DRAFT"),
        ("R", "RECEIVED"),
        ("PR", "PARTIALLY_RECEIVED"),
        ("C", "CANCELLED"),
        ("O", "OVERDUE"),
    ]

    # 'C' is CANCELLED on a purchase invoice and COMPLETED on a sales one.
    # Always reach for this constant; a literal here is the likeliest way to
    # get a purchase figure wrong.
    CANCELLED_STATUSES = ["C"]
    # Not confirmed yet, so not a debt: left out of the khaata with cancelled.
    DRAFT_STATUSES = ["D"]

    PAYMENT_STATUS_CHOICES = [
        ("P", "PAID"),
        ("PP", "PARTIALLY_PAID"),
        ("PEN", "PENDING"),
        ("RF", "REFUNDED"),
        ("C", "CANCELLED")
    ]

    invoice_number = models.CharField(max_length=256, null=True, blank=True)
    business = models.ForeignKey(
        Business, models.CASCADE, related_name='purchase_invoices')
    supplier = models.ForeignKey(
        Supplier, models.DO_NOTHING, related_name='purchase_invoices')
    # Editable, so a purchase entered a day late can carry the day it happened.
    # created_at cannot do this job: it is auto_now_add, so a quarterly purchase
    # target would count a late-entered invoice in the wrong quarter for good.
    date_issued = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    date_due = models.DateField(null=True, blank=True)
    tax = models.JSONField(
        null=True,
        blank=True,
        default=dict,
        help_text='e.g., {"value": 10.0, "type": "percentage" or "amount"}'
    )
    status = models.CharField(
        max_length=256, choices=PURCHASE_INVOICE_STATUS_CHOICES, default=PURCHASE_INVOICE_STATUS_CHOICES[1])
    sub_total = models.FloatField(null=True, blank=True)
    total = models.FloatField(null=True, blank=True)
    goods_received = models.IntegerField(null=True, blank=True)
    delivery = models.DateField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, models.DO_NOTHING, related_name='created_purchase_invoices')
    notes = models.TextField(null=True, blank=True)
    is_restocked = models.BooleanField(default=False)
    is_partially_restocked = models.BooleanField(default=False)

    objects = PurchaseInvoiceManager()

    def __str__(self):
        return f"{self.id}-{self.invoice_number}-{self.total}-{self.status}"

    def adjust_totals(self):
        items = self.invoice_items.all()
        subtotal = sum(item.quantity * item.unit_cost for item in items)

        tax = 0
        if self.tax:
            if self.tax["type"] == "percentage":
                tax = subtotal * (self.tax['value']/100)

            elif self.tax['type'] == "amount":
                tax += self.tax['value']

            else:
                raise Exception("incorrect value")

        self.sub_total = subtotal
        self.total = subtotal + tax
        self.save(update_fields=["sub_total", "total"])

    def is_fulfilled(self):
        for item in self.invoice_items.all():
            if not item.is_restocked:
                self.is_restocked = False
                self.is_partially_restocked = True
                return

        self.is_restocked = True
        self.is_partially_restocked = False
        self.status = 'R'

    def map_to_products(self) -> Dict[int, 'PurchaseInvoiceItem']:
        return {item.product_id: item for item in self.invoice_items.all()}

    def update_restock_flags(self):

        items = self.invoice_items.all()
        if len(items) == 0:
            return

        restocked = 0
        for obj in items:
            if obj.is_partially_restocked:
                self.is_partially_restocked = True

            if obj.is_restocked:
                restocked += 1

        if restocked == len(items):
            self.is_partially_restocked = False
            self.is_restocked = True
            self.status = 'R'


class PurchaseInvoiceItemQuerySet(BaseQuerySet):

    pass


class PurchaseInvoiceItemManager(models.Manager):

    def get_queryset(self):
        return PurchaseInvoiceItemQuerySet(self.model, using=self._db)

    def units_purchased(self, business_id, date_from, date_to,
                        supplier_id=None, product_id=None,
                        product_variant_id=None):
        """
        Units bought over an absolute window, dated on the invoice.

        Gross, and it can only be gross: PurchaseInvoiceItem has no
        returned_quantity, and ReturnedItem points only at SalesInvoiceItem,
        so a purchase return is not recorded anywhere in the system. The
        catalogue entry states this in words so the card can too.

        As with the sales item, product_id filters the Product and
        product_variant_id the variant, because BaseItem.product is a
        ProductVariant.
        """
        return (
            self.get_queryset()
            .filter(purchase_invoice__business_id=business_id)
            .exclude(purchase_invoice__status__in=PurchaseInvoice.CANCELLED_STATUSES)
            .in_range('purchase_invoice__date_issued__date', date_from, date_to)
            .filter_optional(
                purchase_invoice__supplier_id=supplier_id,
                product__base_id=product_id,
                product_id=product_variant_id,
            )
            .aggregate(value=Sum('quantity'))['value'] or 0
        )


class PurchaseInvoiceItem(BaseItem):
    purchase_invoice = models.ForeignKey(
        PurchaseInvoice, models.CASCADE, related_name='invoice_items')
    unit_cost = models.FloatField()
    quantity_received = models.IntegerField(default=0)
    is_restocked = models.BooleanField(default=False)
    is_partially_restocked = models.BooleanField(default=False)

    objects = PurchaseInvoiceItemManager()

    def clean(self):
        super().clean()
        if self.is_restocked and self.is_partially_restocked:
            raise ValidationError(
                "Either 'is_restocked' or 'is_partially_restocked' must be True, but not both.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def morph(self):
        
        '''
        converts purchase invoice item into inventory item
        '''

        try:
            location = self.business.locations.get(is_default=True)
        except Location.DoesNotExist:
            location = None

        return {
            "business_id": self.business.id,
            "inventory_id": self.business.inventory_glance.id,
            "location_id": location.id if location else None,
            "product_id": self.product.id,
            "quantity": self.quantity,
            "track_code": self.track_code,
            "notes": self.notes,
            "last_transaction": generateTransactionId(self),
            "unit_cost": self.unit_cost,
            "quantity_received": self.quantity_received
        }

    def compute_restock_delta(self) -> int:
        """
        Sum up all past restocks to find out how many units remain to add.
        """
        agg = self.restocks.aggregate(total=models.Sum('quantity'))
        prev = agg.get('total') or 0
        return getattr(self, 'quantity') - prev

    def update_restock_flags(self):
        """
        Exactly one of is_restocked / is_partially_restocked should be True.
        """

        full = (self.quantity_received >= self.quantity)
        self.is_restocked = full
        self.is_partially_restocked = not full
        self.save(update_fields=['is_restocked', 'is_partially_restocked'])

    class Meta:
        unique_together = [('purchase_invoice', 'product')]
        # constraints = [
            
        #     models.CheckConstraint(
        #         check=~(models.Q(is_restocked=True) &
        #                 models.Q(is_partially_restocked=True)),
        #         name='is_restocked_and_is_partially_restocked_mutually_exclusive_pii'
        #     )
            
        # ]


class PurchaseReceipt(PaymentReceipt):
    
    purchase_invoice = models.ForeignKey(PurchaseInvoice, on_delete=models.CASCADE, related_name='payment_receipts')
    desc = models.CharField(max_length=512, null=True, blank=True)


class PurchaseInvoiceItemRestock(BaseRestock):
    purchase_invoice = models.ForeignKey(
        PurchaseInvoice, models.CASCADE, related_name='restocks')
    purchase_invoice_item = models.ForeignKey(
        PurchaseInvoiceItem, models.CASCADE, related_name='restocks')

    def __str__(self):
        return f"{self.purchase_invoice.id}: {self.purchase_invoice_item.product.base.name} ({self.purchase_invoice_item.product.name}) x {self.quantity}"


class SalesInvoiceItemDeduction(BaseRestock):
    sales_invoice = models.ForeignKey(
        SalesInvoice, models.CASCADE, related_name='restocks')
    sales_invoice_item = models.ForeignKey(
        SalesInvoiceItem, models.CASCADE, related_name='restocks')

    def __str__(self):
        return f"{self.sales_invoice.id}: {self.sales_invoice_item.product.base.name} ({self.sales_invoice_item.product.name}) x {self.quantity}"


class SalesReservation(BaseRestock):
    sales_invoice = models.ForeignKey(
        SalesInvoice, models.CASCADE, related_name='deductions')
    sales_invoice_item = models.ForeignKey(
        SalesInvoiceItem, models.CASCADE, related_name='deductions')

    def __str__(self):
        return f"{self.sales_invoice.id}: {self.sales_invoice_item.product.base.name} ({self.sales_invoice_item.product.name}) x {self.quantity}"


class PurchaseQuotation(models.Model):

    PQ_STATUSES = [
        ("D", "Draft"),
        ("S", "Sent"),
        ("A", "Accepted"),
        ("R", "Rejected"),
        ("X", "Cancelled"),
    ]

    business = models.ForeignKey(
        Business, on_delete=models.CASCADE, related_name='purchase_quotations')
    quotation_no = models.CharField(max_length=256, null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    status = models.CharField(max_length=256, choices=PQ_STATUSES, default="D")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    notes = models.TextField(null=True, blank=True)


class PurchaseQuotationItem(BaseItem):
    purchase_quotation = models.ForeignKey(
        PurchaseQuotation, on_delete=models.CASCADE, related_name='items'
    )
    supplier = models.ForeignKey(Supplier, models.CASCADE, related_name='quotation_items')
    unit_price = models.FloatField(null=True, blank=True)
    is_fulfilled = models.BooleanField(default=False)


# SalesQuotation*
# Payment*
# Inter Account Transfer
# Bank Reconciliations
# Witholding Tax
# Debit Notes
# Inventory Write Offs
# Production Orders
# Employees
# Employee Payslips
# Master Account Functionality
# Folders
# Financial Reports


class ReturnedItemsQuerySet(BaseQuerySet):
    pass


class ReturnedItemManager(models.Manager):

    def get_queryset(self):
        return ReturnedItemsQuerySet(self.model)

    def total_returned_items(self, business_id, num_days=None):
        queryset = self.get_queryset().for_business(business_id)

        if num_days:
            queryset = queryset.in_period(num_days)

        return queryset.count()


class ReturnedItem(models.Model):

    RETURN_TYPES = [
        ("to_invoice", "To Invoice"),
        ("to_inventory", "To Inventory"),
    ]

    business = models.ForeignKey(
        Business, 
        on_delete=models.CASCADE, 
        related_name='returned_items'
    )
    invoice_item = models.ForeignKey(
        SalesInvoiceItem,
        on_delete=models.CASCADE,
        related_name='returns',
    )
    reason = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    quantity = models.IntegerField(default=0)
    return_type = models.CharField(
        max_length=256, choices=RETURN_TYPES, 
        null=True, blank=True
    )
    is_damaged = models.BooleanField(default=False)
    objects = ReturnedItemManager()

    @property
    def is_returned(self):
        if self.return_type == None:
            return False
        
        return True

    def __str__(self):
        return f"Return for {self.invoice_item.product.base.name} ({self.invoice_item.product.name}) from Invoice {self.invoice_item.sales_invoice.invoice_number} on {local_date(self.created_at)}"

