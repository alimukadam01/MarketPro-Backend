"""
The backlog: photos of a shop's paper records, waiting to be entered.

An owner uploads pages (UploadBatch); each image becomes one BacklogEntry.
A data entry account claims the next one, enters what is on it - the
records it produces are BacklogOutputs - and submits it. The owner then
reviews it (BacklogReview): approves it, or sends it back with a reason.

    pending -> in_progress -> entered -> verified
                     \\-> cant_read      (back to pending when re-photographed)
    entered --send back--> pending       (at the top, with the reason)

plans/backlog-data-entry.md.
"""
from django.conf import settings
from django.db import models

from root.models import BaseQuerySet, Business


class BacklogType:
    SALES_INVOICE = 'sales_invoice'
    PURCHASE_INVOICE = 'purchase_invoice'
    PRODUCT_LIST = 'product_list'
    PRICE_LIST = 'price_list'
    STOCK_LIST = 'stock_list'

    CHOICES = [
        (SALES_INVOICE, 'Sales Invoice'),
        (PURCHASE_INVOICE, 'Purchase Invoice'),
        (PRODUCT_LIST, 'Product List'),
        (PRICE_LIST, 'Price List'),
        (STOCK_LIST, 'Stock List'),
    ]
    # A page of many records, entered one at a time and marked complete.
    LISTS = (PRODUCT_LIST, PRICE_LIST, STOCK_LIST)
    INVOICES = (SALES_INVOICE, PURCHASE_INVOICE)


class Priority:
    NORMAL = 'normal'
    HIGH = 'high'
    CHOICES = [(NORMAL, 'Normal'), (HIGH, 'High')]


class UploadBatch(models.Model):
    """One upload: the pages a person sent together, and what they are."""

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name='backlog_batches')
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='backlog_batches')
    type = models.CharField(max_length=32, choices=BacklogType.CHOICES)
    book_label = models.CharField(max_length=256, blank=True, default='')
    priority = models.CharField(max_length=16, choices=Priority.CHOICES, default=Priority.NORMAL)
    # Today's pages: entered first, by the end of the next working day.
    is_daily = models.BooleanField(default=False)
    notes = models.TextField(blank=True, default='')
    # What the upload held that was not an image (a PDF, a nested zip).
    skipped_files = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.id}-{self.type}-{self.book_label}'


class BacklogEntryQuerySet(BaseQuerySet):

    def for_business(self, business_id):
        return self.filter(business_id=business_id)


class BacklogEntry(models.Model):
    """One page to enter."""

    PENDING = 'pending'
    IN_PROGRESS = 'in_progress'
    ENTERED = 'entered'
    VERIFIED = 'verified'
    CANT_READ = 'cant_read'
    STATUS_CHOICES = [
        (PENDING, 'Pending'),
        (IN_PROGRESS, 'In progress'),
        (ENTERED, 'Waiting for review'),
        (VERIFIED, 'Verified'),
        (CANT_READ, "Can't read"),
    ]

    # Kept for the old name, read by the admin and old migrations.
    TYPE_CHOICES = BacklogType.CHOICES

    business = models.ForeignKey(
        Business, on_delete=models.CASCADE, related_name='backlog_entries'
    )
    batch = models.ForeignKey(
        UploadBatch, on_delete=models.SET_NULL, null=True, blank=True, related_name='entries')
    # Null once verified: an invoice's photo moves onto the invoice, a list's
    # is deleted (§9). The row stays as the record of the work.
    image = models.ImageField(upload_to='backlog', null=True, blank=True)
    type = models.CharField(max_length=256, choices=BacklogType.CHOICES)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=PENDING)

    # Copied from the batch, so the queue can be ordered and read without it.
    book_label = models.CharField(max_length=256, blank=True, default='')
    priority = models.CharField(max_length=16, choices=Priority.CHOICES, default=Priority.NORMAL)
    is_daily = models.BooleanField(default=False)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='created_backlogs'
    )
    claimed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='claimed_backlogs')
    claimed_at = models.DateTimeField(null=True, blank=True)
    entered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='entered_backlogs')
    entered_at = models.DateTimeField(null=True, blank=True)
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='verified_backlogs')
    verified_at = models.DateTimeField(null=True, blank=True)

    notes = models.TextField(null=True, blank=True)
    cant_read_note = models.TextField(blank=True, default='')
    # The reason given the last time it was sent back, shown to whoever
    # picks it up again.
    review_note = models.TextField(blank=True, default='')
    rejection_count = models.PositiveIntegerField(default=0)
    seconds_spent = models.PositiveIntegerField(default=0)
    # The glossary version in force when it was entered (§14).
    glossary_version = models.PositiveIntegerField(null=True, blank=True)
    # A list page is many records; this says the person finished the page.
    completed_list = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = BacklogEntryQuerySet.as_manager()

    class Meta:
        verbose_name_plural = 'backlog entries'
        indexes = [models.Index(fields=['business', 'status'])]

    @property
    def is_list(self):
        return self.type in BacklogType.LISTS

    def __str__(self):
        return f"{self.id}-{self.type}-{self.status}"


class BacklogOutput(models.Model):
    """
    A record entered from a page. Exactly one of the record links is set -
    the same shape as InvoiceImage and PartyPayment.

    `created` is False when the entry changed a row that already existed
    (a price or a count on an existing stock row): deleting the output then
    must not delete that row.
    """

    entry = models.ForeignKey(BacklogEntry, on_delete=models.CASCADE, related_name='outputs')
    sales_invoice = models.ForeignKey(
        'sales.SalesInvoice', on_delete=models.CASCADE, null=True, blank=True,
        related_name='backlog_outputs')
    purchase_invoice = models.ForeignKey(
        'sales.PurchaseInvoice', on_delete=models.CASCADE, null=True, blank=True,
        related_name='backlog_outputs')
    product = models.ForeignKey(
        'root.Product', on_delete=models.CASCADE, null=True, blank=True,
        related_name='backlog_outputs')
    inventory_item = models.ForeignKey(
        'inventory.InventoryItem', on_delete=models.CASCADE, null=True, blank=True,
        related_name='backlog_outputs')
    created = models.BooleanField(default=True)
    # What the person typed from the page, for showing and editing the line
    # without reading the row back (which would show figures they must not
    # see, like the stock already on hand).
    entered = models.JSONField(default=dict, blank=True)
    # For a row that existed before (created=False): its values before this
    # entry changed them, so deleting the output puts them back.
    previous = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='backlog_outputs')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                name='backlog_output_exactly_one_record',
                condition=(
                    models.Q(sales_invoice__isnull=False, purchase_invoice__isnull=True,
                             product__isnull=True, inventory_item__isnull=True)
                    | models.Q(sales_invoice__isnull=True, purchase_invoice__isnull=False,
                               product__isnull=True, inventory_item__isnull=True)
                    | models.Q(sales_invoice__isnull=True, purchase_invoice__isnull=True,
                               product__isnull=False, inventory_item__isnull=True)
                    | models.Q(sales_invoice__isnull=True, purchase_invoice__isnull=True,
                               product__isnull=True, inventory_item__isnull=False)
                ),
            ),
        ]

    @property
    def kind(self):
        for name in ('sales_invoice', 'purchase_invoice', 'product', 'inventory_item'):
            if getattr(self, f'{name}_id'):
                return name
        return None

    @property
    def record(self):
        kind = self.kind
        return getattr(self, kind) if kind else None

    def __str__(self):
        return f'{self.entry_id}->{self.kind}#{getattr(self, f"{self.kind}_id", None)}'


class BacklogNewRecord(models.Model):
    """
    A customer, supplier, product or product variant created from a page
    because the page named one MarketPro did not have (BUGS.md #006). Kept
    so the owner reviewing the page sees what was added to the shop's
    records. Products and variants come only from a stock page
    (backlog/new_products.py); a variant here is one added to a product
    MarketPro already had - a new product's own variants come with it.

    Not a BacklogOutput: an output is what the page's own content became -
    what Submit requires and approval acts on. A new customer is neither.
    Exactly one link is set; deleting the record deletes this row with it.
    """

    entry = models.ForeignKey(BacklogEntry, on_delete=models.CASCADE, related_name='new_records')
    customer = models.ForeignKey(
        'root.Customer', on_delete=models.CASCADE, null=True, blank=True, related_name='+')
    supplier = models.ForeignKey(
        'root.Supplier', on_delete=models.CASCADE, null=True, blank=True, related_name='+')
    product = models.ForeignKey(
        'root.Product', on_delete=models.CASCADE, null=True, blank=True, related_name='+')
    variant = models.ForeignKey(
        'root.ProductVariant', on_delete=models.CASCADE, null=True, blank=True, related_name='+')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                name='backlog_new_record_exactly_one',
                condition=(
                    models.Q(customer__isnull=False, supplier__isnull=True, product__isnull=True,
                             variant__isnull=True)
                    | models.Q(customer__isnull=True, supplier__isnull=False, product__isnull=True,
                               variant__isnull=True)
                    | models.Q(customer__isnull=True, supplier__isnull=True, product__isnull=False,
                               variant__isnull=True)
                    | models.Q(customer__isnull=True, supplier__isnull=True, product__isnull=True,
                               variant__isnull=False)
                ),
            ),
        ]

    @property
    def kind(self):
        for name in ('customer', 'supplier', 'product', 'variant'):
            if getattr(self, f'{name}_id'):
                return name
        return None

    def __str__(self):
        return f'{self.entry_id}->new {self.kind}#{getattr(self, f"{self.kind}_id", None)}'


class BacklogReview(models.Model):
    """One review of an entered page, kept even after it is redone, so
    accuracy (approved / reviewed) and the corrections survive (§12, §14)."""

    APPROVED = 'approved'
    SENT_BACK = 'sent_back'
    OUTCOMES = [(APPROVED, 'Approved'), (SENT_BACK, 'Sent back')]

    entry = models.ForeignKey(BacklogEntry, on_delete=models.CASCADE, related_name='reviews')
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='backlog_reviews')
    # Who had entered it - accuracy is theirs.
    entered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='reviewed_backlogs')
    outcome = models.CharField(max_length=16, choices=OUTCOMES)
    note = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.entry_id}-{self.outcome}'


# ── Glossary (§13) ───────────────────────────────────────────────────────────
#
# The shop's shorthand ("S.W" = Sweet White, "Bhai jan" = Hassan Builders),
# shown beside every page being entered. Versioned without ever editing a
# row: a change closes the old row (valid_to = v) and opens a new one
# (valid_from = v), so version v is exactly the rows with
# valid_from <= v < valid_to - which is what an entry records it was made
# with (BacklogEntry.glossary_version), for the AI work later (§14).

class GlossaryKind:
    CHOICES = [
        ('shorthand', 'Shorthand'),
        ('shade', 'Shade'),
        ('unit', 'Unit'),
        ('product', 'Product'),
        ('customer', 'Customer'),
        ('supplier', 'Supplier'),
        ('payment', 'Payment'),
    ]


class Glossary(models.Model):
    """A business's glossary version counter."""

    business = models.OneToOneField(Business, on_delete=models.CASCADE, related_name='glossary')
    version = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)


class GlossaryEntryQuerySet(models.QuerySet):

    def at_version(self, version):
        return self.filter(valid_from__lte=version).filter(
            models.Q(valid_to__isnull=True) | models.Q(valid_to__gt=version))

    def current(self):
        return self.filter(valid_to__isnull=True)


class GlossaryEntry(models.Model):

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name='glossary_entries')
    term = models.CharField(max_length=128)
    meaning = models.CharField(max_length=256)
    kind = models.CharField(max_length=16, choices=GlossaryKind.CHOICES, default='shorthand')
    customer = models.ForeignKey('root.Customer', on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='+')
    supplier = models.ForeignKey('root.Supplier', on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='+')
    product = models.ForeignKey('root.Product', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='+')
    valid_from = models.PositiveIntegerField()
    # Exclusive; null while it is the current row.
    valid_to = models.PositiveIntegerField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='+')
    # The page that prompted it, when it came from a suggestion (§14).
    source_entry = models.ForeignKey(BacklogEntry, on_delete=models.SET_NULL, null=True,
                                     blank=True, related_name='glossary_entries')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = GlossaryEntryQuerySet.as_manager()

    class Meta:
        verbose_name_plural = 'glossary entries'
        indexes = [models.Index(fields=['business', 'valid_to'])]

    def __str__(self):
        return f'{self.term} = {self.meaning}'


class GlossarySuggestion(models.Model):

    PENDING = 'pending'
    APPROVED = 'approved'
    REJECTED = 'rejected'
    STATUSES = [(PENDING, 'Pending'), (APPROVED, 'Approved'), (REJECTED, 'Rejected')]

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name='glossary_suggestions')
    entry = models.ForeignKey(BacklogEntry, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='glossary_suggestions')
    suggested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name='+')
    term = models.CharField(max_length=128)
    meaning = models.CharField(max_length=256)
    kind = models.CharField(max_length=16, choices=GlossaryKind.CHOICES, default='shorthand')
    customer = models.ForeignKey('root.Customer', on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='+')
    supplier = models.ForeignKey('root.Supplier', on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='+')
    product = models.ForeignKey('root.Product', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='+')
    note = models.TextField(blank=True, default='')
    status = models.CharField(max_length=16, choices=STATUSES, default=PENDING)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name='+')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    glossary_entry = models.ForeignKey(GlossaryEntry, on_delete=models.SET_NULL, null=True,
                                       blank=True, related_name='suggestions')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.term} = {self.meaning} ({self.status})'
