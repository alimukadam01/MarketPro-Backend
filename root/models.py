from django.db import models
from django.db.models.functions import TruncDate
from calendar import monthrange
from datetime import date, datetime, timedelta
from django.conf import settings

# Create your models here.

class BaseQuerySet(models.QuerySet):

    def for_business(self, business_id):
        return self.filter(business_id=business_id)

    def in_period(self, days):
        start = datetime.today() - timedelta(days=days)
        return self.filter(created_at__gte = start)
    
    def monthly_trend(self, business_id, field):
        today = date.today()
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
    created_at = models.DateTimeField(auto_now_add = True)

    def __str__(self):
        return f"{self.name}"
    

class Category(models.Model):

    name = models.CharField(max_length=256) 
    desc = models.CharField(max_length=1000, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add = True)

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
    logo = models.ImageField(upload_to="business_logos/", null=True, blank=True)
    is_active = models.BooleanField(default=False)

    def __str__(self):
        return f"{ self.name }"


class BusinessConfig(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, models.CASCADE, related_name='business_config')
    is_sales_enabled = models.BooleanField(default=True)
    is_purchases_enabled = models.BooleanField(default=True)
    is_projects_enabled = models.BooleanField(default=False)
    is_inventory_enabled = models.BooleanField(default=True)


class CustomerQuerySet(BaseQuerySet):
    pass


class CustomerManager(models.Manager):

    def get_queryset(self):
        return CustomerQuerySet(self.model)

    def total_customers(self, business_id, num_days=None):

        if num_days:
            return self.get_queryset().for_business(business_id).in_period(num_days).count()

        return self.get_queryset().for_business(business_id).count()


class Customer(models.Model):

    name = models.CharField(max_length=256)
    business = models.ForeignKey(Business, models.CASCADE, related_name='customers')
    phone = models.CharField(max_length=256, null=True, blank=True)
    email = models.EmailField(null=True, blank=True)
    address = models.TextField(null=True, blank=True)
    city = models.ForeignKey(City, models.DO_NOTHING)
    created_at = models.DateTimeField(auto_now_add = True)
    updated_at = models.DateTimeField(auto_now = True)
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

    business = models.ForeignKey(Business, models.CASCADE, related_name='locations')
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


class Product(models.Model):
    business = models.ForeignKey(Business, models.CASCADE, related_name='products')
    name = models.CharField(max_length=256)
    desc = models.TextField(null=True, blank=True)
    unit = models.ForeignKey(Unit, models.DO_NOTHING)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ProductManager()

    def __str__(self):
        return f"{self.name}"


class BaseItem(models.Model):

    business = models.ForeignKey(Business, models.CASCADE)
    product = models.ForeignKey(Product, models.CASCADE)
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
    business = models.ForeignKey(Business, models.CASCADE, related_name='suppliers')
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

    def monthly_expenses_trend(self, business_id):
        return self.get_queryset().monthly_trend(business_id, 'amount')

class Expense(models.Model):
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name='expenses')
    name = models.CharField(max_length=256)
    desc = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    amount = models.FloatField(default=0)

    objects = ExpenseManager()
