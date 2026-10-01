from django.db import models
from django.db.models.functions import TruncDate
from calendar import monthrange
from datetime import date, datetime, timedelta
from django.conf import settings
from django.utils import timezone

# Create your models here.


class BaseQuerySet(models.QuerySet):

    def for_business(self, business_id):
        return self.filter(business_id=business_id)

    def in_period(self, days):
        # timezone.now() is aware, so this compares like with like. A naive
        # datetime here would be read as UTC and shift the window.
        start = timezone.now() - timedelta(days=days)
        return self.filter(created_at__gte=start)

    def in_range(self, date_lookup, date_from, date_to):
        """
        An inclusive window between two absolute dates.

        The caller passes the whole lookup, because only the app that owns the
        model knows the field's type. A DateTimeField needs '<field>__date' so
        the day is decided in Asia/Karachi rather than UTC; a DateField takes
        the bare name, and '__date' on one raises FieldError.

        Not the same thing as in_period(days), which is relative to now and
        always reads created_at.
        """
        return self.filter(**{
            f'{date_lookup}__gte': date_from,
            f'{date_lookup}__lte': date_to,
        })

    def filter_optional(self, **kwargs):
        """
        Applies only the kwargs whose value is not None, so a caller can pass
        every filter it supports and let the unset ones fall away.

        None means 'not filtered'. Matching a NULL column has to be asked for
        explicitly with an __isnull lookup.
        """
        return self.filter(**{
            key: value for key, value in kwargs.items() if value is not None
        })

    def monthly_trend(self, business_id, field):
        today = timezone.localdate()
        year = today.year
        month = today.month

        # 1. Get correct number of days in the month
        _, days_in_month = monthrange(year, month)

        # 2. Aggregate sales by date
        qs = (
            self
            .for_business(business_id)
            .filter(
                created_at__year=year,
                created_at__month=month
            )
            .annotate(day=TruncDate("created_at"))
            .values("day")
            .annotate(total=models.Sum(field))
        )

        # 3. Convert queryset to lookup map
        sales_map = {
            item["day"]: float(item["total"])
            for item in qs
        }

        # 4. Build full month result
        result = []

        for day_num in range(1, days_in_month + 1):
            current_date = date(year, month, day_num)

            if current_date > today:
                total_sales = 0.0
            else:
                total_sales = sales_map.get(current_date, 0.0)

            result.append({
                "day": current_date.isoformat(),
                "value": total_sales
            })

        return result


class City(models.Model):

    name = models.CharField(max_length=256)
    postal_code = models.CharField(max_length=256)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name}"


class Category(models.Model):

    name = models.CharField(max_length=256)
    desc = models.CharField(max_length=1000, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name}"


class Unit(models.Model):

    name = models.CharField(max_length=256)
    abv = models.CharField(max_length=256, null=True, blank=True)

    def __str__(self):
        return f"{ self.name }"


class Business(models.Model):

    name = models.CharField(max_length=256)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="businesses"
    )
    phone = models.CharField(max_length=256)
    address = models.TextField(null=True, blank=True)
    logo = models.ImageField(
        upload_to="business_logos/", null=True, blank=True)
    sku_counter = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=False)

    def __str__(self):
        return f"{ self.name }"


class Employee(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='emp_records')
    business = models.ForeignKey(
        Business, models.CASCADE, related_name='emp_records')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, models.CASCADE)

    class Meta:
        unique_together = [("business", "user")]


class BusinessConfig(models.Model):
    business = models.OneToOneField(
        Business, models.CASCADE, related_name='config')
    sales = models.BooleanField(default=True)
    purchases = models.BooleanField(default=True)
    projects = models.BooleanField(default=False)
    inventory = models.BooleanField(default=True)
    returned_items = models.BooleanField(default=True)
    quotations = models.BooleanField(default=True)
    accounting = models.BooleanField(default=False)
    # Paid add-on, off until it is bought. This flag is not optional: an admin's
    # permissions are derived from this model, and any module in
    # EmployeeAccess.all_modules without a field here is granted unconditionally.
    targets = models.BooleanField(default=False)

    def __str__(self):
        return f"Config for {self.business.name}"


class EmployeeAccess(models.Model):
    """
    Stores per-module CRUD permissions for an employee within a specific business.
    Admins always have full access; this model only applies to role='employee' users.

    permissions JSON structure:
    {
        "sales":         {"view": bool, "create": bool, "edit": bool, "delete": bool},
        "purchases":     {"view": bool, "create": bool, "edit": bool, "delete": bool},
        "inventory":     {"view": bool, "create": bool, "edit": bool, "delete": bool},
        "products":      {"view": bool, "create": bool, "edit": bool, "delete": bool},
        "customers":     {"view": bool, "create": bool, "edit": bool, "delete": bool},
        "suppliers":     {"view": bool, "create": bool, "edit": bool, "delete": bool},
        "locations":     {"view": bool, "create": bool, "edit": bool, "delete": bool},
        "expenses":      {"view": bool, "create": bool, "edit": bool, "delete": bool},
        "projects":      {"view": bool, "create": bool, "edit": bool, "delete": bool},
        "quotations":    {"view": bool, "create": bool, "edit": bool, "delete": bool},
        "returned_items":{"view": bool, "create": bool, "edit": bool, "delete": bool},
        "accounting":    {"view": bool, "create": bool, "edit": bool, "delete": bool},
        "targets":       {"view": bool, "create": bool, "edit": bool, "delete": bool},
    }
    """

    all_modules = [
        "sales",
        "purchases",
        "inventory",
        "products",
        "customers",
        "suppliers",
        "locations",
        "expenses",
        "projects",
        "quotations",
        "returned_items",
        "backlog_entries",
        "accounting",
        "targets",
    ]

    employee = models.OneToOneField(
        Employee, on_delete=models.CASCADE, related_name="access")
    permissions = models.JSONField(default=dict)

    def __str__(self):
        return f"{self.employee.user.email} @ {self.employee.business.name}"


### the counter-sale customer every business is given, matched by name when a
### walk-in invoice is downloaded so the buyer can be captured then.
### The client matches the same string — inventrify-client-react/services/utils.js,
### WALK_IN_CUSTOMER_NAME. Changing it here without changing it there turns walk-in
### detection off.
WALK_IN_CUSTOMER_NAME = "Walk-In Customer"


class CustomerQuerySet(BaseQuerySet):
    pass


class CustomerManager(models.Manager):

    def get_queryset(self):
        return CustomerQuerySet(self.model)

    def total_customers(self, business_id, num_days=None):

        if num_days:
            return self.get_queryset().for_business(business_id).in_period(num_days).count()

        return self.get_queryset().for_business(business_id).count()

    def create_walk_in(self, business_id):
        """
        The counter sale. Every business gets one so an invoice can be raised
        before the buyer is known; their details are captured later, when the
        invoice is downloaded.

        Returns None when there is no city to attach it to — the business is
        still created, it just has no placeholder, which is the same position
        as a business that predates this.
        """
        # Customer.city is NOT NULL and City is a global table with no default
        # and no business scoping, so the placeholder is pinned to the lowest
        # id. It is never displayed: the buyer's real city is captured on the
        # customer this one is replaced by.
        city_id = City.objects.order_by('id').values_list('id', flat=True).first()
        if not city_id:
            return None

        customer, _ = self.get_queryset().get_or_create(
            business_id=business_id,
            name=WALK_IN_CUSTOMER_NAME,
            defaults={'city_id': city_id},
        )
        return customer


class Customer(models.Model):

    name = models.CharField(max_length=256)
    business = models.ForeignKey(
        Business, models.CASCADE, related_name='customers')
    phone = models.CharField(max_length=256, null=True, blank=True)
    email = models.EmailField(null=True, blank=True)
    address = models.TextField(null=True, blank=True)
    city = models.ForeignKey(City, models.DO_NOTHING)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    notes = models.TextField(null=True, blank=True)
    total_sales = models.FloatField(null=True, blank=True)

    objects = CustomerManager()

    def __str__(self):
        return f"{self.email}"


class LocationQuerySet(BaseQuerySet):
    pass


class LocationManager(models.Manager):

    def get_queryset(self):
        return LocationQuerySet(self.model)

    def total_locations(self, business_id):
        return self.get_queryset().for_business(business_id).count()


class Location(models.Model):

    business = models.ForeignKey(
        Business, models.CASCADE, related_name='locations'
    )
    name = models.CharField(max_length=256)
    address = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    is_default = models.BooleanField(default=False)

    objects = LocationManager()

    def __str__(self):
        return f"{self.name}"


class ProductQuerySet(BaseQuerySet):
    pass


class ProductManager(models.Manager):

    def get_queryset(self):
        return ProductQuerySet(self.model)

    def total_products(self, business_id, num_days=None):

        if num_days:
            return self.get_queryset().for_business(business_id).in_period(num_days).count()

        return self.get_queryset().for_business(business_id).count()


class ProductVariantType(models.Model):
    name = models.CharField(max_length=256)

    def __str__(self):
        return self.name


class Product(models.Model):
    business = models.ForeignKey(
        Business, models.CASCADE, related_name='products')
    name = models.CharField(max_length=256)
    code = models.CharField(max_length=256, null=True, blank=True)
    desc = models.TextField(null=True, blank=True)
    unit = models.ForeignKey(Unit, models.DO_NOTHING)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ProductManager()

    def __str__(self):
        return f"{self.name}"


class ProductVariant(models.Model):

    name = models.CharField(max_length=256, null=True, blank=True)
    base = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name='variants')
    sku = models.CharField(max_length=256)
    attributes = models.JSONField(default=dict, blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.base.name} - {self.name}"


class BaseItem(models.Model):

    business = models.ForeignKey(Business, models.CASCADE)
    product = models.ForeignKey(
        ProductVariant, models.CASCADE, null=True, blank=True)
    quantity = models.IntegerField(default=0)
    track_code = models.CharField(max_length=256, null=True, blank=True)
    notes = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class SupplierQuerySet(BaseQuerySet):
    pass


class SupplierManager(models.Manager):

    def get_queryset(self):
        return SupplierQuerySet(self.model)

    def total_suppliers(self, business_id, num_days=None):

        if num_days:
            return self.get_queryset().for_business(business_id).in_period(num_days).count()

        return self.get_queryset().for_business(business_id).count()


class Supplier(models.Model):
    business = models.ForeignKey(
        Business, models.CASCADE, related_name='suppliers')
    name = models.CharField(max_length=256)
    business_name = models.CharField(max_length=256, null=True, blank=True)
    phone = models.CharField(max_length=256, null=True, blank=True)
    email = models.EmailField(null=True, blank=True)
    notes = models.TextField(null=True, blank=True)

    objects = SupplierManager()

    def __str__(self):
        return f"{self.business_name}: {self.name}"


class ExpenseQuerySet(BaseQuerySet):
    pass


class ExpenseManager(models.Manager):

    def get_queryset(self):
        return ExpenseQuerySet(self.model)

    def total_expenses(self, business_id, num_days=None):

        if num_days:
            return self.get_queryset().for_business(business_id).in_period(num_days).count()

        return self.get_queryset().for_business(business_id).count()

    def total_expense_amount(self, business_id, num_days=None):
        queryset = self.get_queryset().for_business(business_id)

        if num_days:
            queryset = queryset.in_period(num_days)

        return queryset.aggregate(total=models.Sum("amount"))["total"] or 0

    def expense_amount_in_range(self, business_id, date_from, date_to,
                                category=None):
        """
        The absolute-window sibling of total_expense_amount, written for the
        targets module.

        Dated on created_at because that is the only date Expense has: no
        editable date, no updated_at, no created_by. So an expense typed a week
        after the money went out lands in the wrong month, and an expense
        target can never be narrowed to a person. Both limits are stated on the
        card rather than hidden.
        """
        return (
            self.get_queryset()
            .for_business(business_id)
            .in_range('created_at__date', date_from, date_to)
            .filter_optional(category=category)
            .aggregate(value=models.Sum('amount'))['value'] or 0
        )

    def monthly_expenses_trend(self, business_id):
        return self.get_queryset().monthly_trend(business_id, 'amount')


class Expense(models.Model):

    CATEGORY_CHOICES = [
        ("kiraya", "Rent (Kiraya)"),
        ("bijli", "Electricity (Bijli)"),
        ("tankhwa", "Salaries (Tankhwa)"),
        ("transport", "Transport"),
        ("mutafarriq", "Miscellaneous (Mutafarriq)"),
    ]

    # Salaries are recorded as an expense in this category rather than as their
    # own transaction type, but the daily summary still reports them on their
    # own line (BRD 6.9). Named here so the reporting code does not carry a
    # bare string.
    SALARY_CATEGORY = "tankhwa"

    business = models.ForeignKey(
        Business, on_delete=models.CASCADE, related_name='expenses')
    name = models.CharField(max_length=256)
    category = models.CharField(
        max_length=256, choices=CATEGORY_CHOICES, null=True, blank=True)
    desc = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    amount = models.FloatField(default=0)

    objects = ExpenseManager()
