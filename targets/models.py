from django.conf import settings
from django.db import models

from root.models import Business, BaseQuerySet


class TargetQuerySet(BaseQuerySet):

    pass


class TargetManager(models.Manager):

    def get_queryset(self):
        return TargetQuerySet(self.model, using=self._db)

    def for_business(self, business_id):
        return self.get_queryset().for_business(business_id)


class Target(models.Model):
    """
    A number a business intends to reach over a period.

    Nothing measured is stored here. Not the resolved window, not the figure
    achieved, not the status. A rolling window re-resolves every day and there
    is no scheduler in this project to recompute anything, so every figure is
    computed on read by targets.utils.progress().
    """

    DATA_POINT_KIND_CHOICES = [
        ('base', 'Base'),
        ('composite', 'Composite'),
    ]

    SCOPE_TYPE_CHOICES = [
        ('business', 'Whole Business'),
        ('subject', 'One Subject'),
    ]

    SUBJECT_TYPE_CHOICES = [
        ('customer', 'Customer'),
        ('supplier', 'Supplier'),
        ('product', 'Product'),
        ('product_variant', 'Product Variant'),
        ('employee', 'Employee'),
    ]

    PERIOD_TYPE_CHOICES = [
        ('month', 'Month'),
        ('quarter', 'Quarter'),
        ('year', 'Year'),
        ('custom', 'Custom Range'),
        ('rolling', 'Rolling Window'),
    ]

    # All seven are listed so a later phase needs no AlterField. The serializer
    # accepts only 'threshold' until the rest are actually implemented.
    RULE_TYPE_CHOICES = [
        ('threshold', 'Threshold'),
        ('ratio_to_target', 'Ratio To Target'),
        ('slab', 'Slab / Ladder'),
        ('prior_period', 'Prior Period Comparison'),
        ('elapsed_time', 'Elapsed Time'),
        ('status_flag', 'Status Flag'),
        ('combination', 'Combination'),
    ]

    business = models.ForeignKey(
        Business, on_delete=models.CASCADE, related_name='targets')
    name = models.CharField(max_length=256)

    data_point_kind = models.CharField(
        max_length=256, choices=DATA_POINT_KIND_CHOICES, default='base')
    # Validated against targets.catalogue.CATALOGUE rather than a choices list,
    # so adding a data point never needs a migration.
    data_point = models.CharField(max_length=256)

    scope_type = models.CharField(
        max_length=256, choices=SCOPE_TYPE_CHOICES, default='business')
    subject_type = models.CharField(
        max_length=256, choices=SUBJECT_TYPE_CHOICES, null=True, blank=True)
    # A plain integer, not a ForeignKey. Every on_delete would be wrong here:
    # CASCADE would delete the target because a customer was deleted, SET_NULL
    # would silently widen it from one party to all, PROTECT would block the
    # deletion, DO_NOTHING would dangle. The label below is captured at save so
    # the card stays readable after the subject is gone.
    subject_id = models.PositiveIntegerField(null=True, blank=True)
    subject_label = models.CharField(max_length=256, null=True, blank=True)

    period_type = models.CharField(
        max_length=256, choices=PERIOD_TYPE_CHOICES)
    period_year = models.PositiveIntegerField(null=True, blank=True)
    period_month = models.PositiveIntegerField(null=True, blank=True)
    period_quarter = models.PositiveIntegerField(null=True, blank=True)
    date_from = models.DateField(null=True, blank=True)
    date_to = models.DateField(null=True, blank=True)
    rolling_days = models.PositiveIntegerField(null=True, blank=True)

    target_value = models.FloatField(default=0)
    rule_type = models.CharField(
        max_length=256, choices=RULE_TYPE_CHOICES, default='threshold')

    # Free text. The module never reads, interprets or applies it.
    outcome = models.TextField(null=True, blank=True)
    notes = models.TextField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='created_targets')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TargetManager()

    def __str__(self):
        return f"{self.id}-{self.name}-{self.data_point}"


class TargetFilter(models.Model):
    """
    One narrowing applied to a target's data point.

    value_id holds an entity's primary key and value_text a choice value; only
    one is ever set. label is denormalised at save time, so a card renders
    "Jotun" with no join and keeps rendering it after the supplier is deleted.
    The aggregate then filters a primary key that no longer exists and returns
    zero, which is honest: the target visibly stops moving.
    """

    FILTER_DIMENSION_CHOICES = [
        ('customer', 'Customer'),
        ('supplier', 'Supplier'),
        ('product', 'Product'),
        ('product_variant', 'Product Variant'),
        ('entered_by', 'Entered By'),
        ('invoice_status', 'Invoice Status'),
        ('payment_method', 'Payment Method'),
        ('expense_category', 'Expense Category'),
    ]

    target = models.ForeignKey(
        Target, on_delete=models.CASCADE, related_name='filters')
    dimension = models.CharField(
        max_length=256, choices=FILTER_DIMENSION_CHOICES)
    value_id = models.PositiveIntegerField(null=True, blank=True)
    value_text = models.CharField(max_length=256, null=True, blank=True)
    label = models.CharField(max_length=256)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # One filter per dimension for now. Dropping this is how multi-value
        # filters arrive later, without changing any read path.
        unique_together = [('target', 'dimension')]

    def __str__(self):
        return f"{self.id}-{self.dimension}-{self.label}"


class ManualDataPointQuerySet(BaseQuerySet):

    pass


class ManualDataPointManager(models.Manager):

    def get_queryset(self):
        return ManualDataPointQuerySet(self.model, using=self._db)

    def for_business(self, business_id):
        return self.get_queryset().for_business(business_id)


class ManualDataPoint(models.Model):
    """
    A figure typed in by hand, shown on the dashboard for reference only.

    Its purpose is to sit beside MarketPro's own number: a dealer can record
    what his supplier claims his quarterly purchases were and see where the two
    disagree.

    It has no relationship to Target, and that is deliberate. No foreign key,
    no entry in the catalogue, no code path from Target.data_point reaches it.
    That is how "a manual value can never be measured by a target" is enforced
    by the schema rather than by a validator somebody can forget.
    """

    VALUE_TYPE_CHOICES = [
        ('number', 'Number'),
        ('amount', 'Amount (PKR)'),
    ]

    business = models.ForeignKey(
        Business, on_delete=models.CASCADE, related_name='manual_data_points')
    name = models.CharField(max_length=256)
    # A FloatField because core.admin_masking maps only FloatField and
    # IntegerField; anything else fails its startup check when registered.
    value = models.FloatField(default=0)
    value_type = models.CharField(
        max_length=256, choices=VALUE_TYPE_CHOICES, default='number')
    as_of_date = models.DateField(null=True, blank=True)
    notes = models.TextField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='created_manual_data_points')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ManualDataPointManager()

    def __str__(self):
        return f"{self.id}-{self.name}"
