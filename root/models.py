from django.db import models
from datetime import datetime, timedelta
from django.conf import settings

# Create your models here.

class BaseQuerySet(models.QuerySet):

    def for_business(self, business_id):
        return self.filter(business_id=business_id)

    def in_period(self, days):
        start = datetime.today() - timedelta(days=days)
        return self.filter(created_at__gte = start)


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

    def __str__(self):
        return f"{self.email}"


class Location(models.Model):

    business = models.ForeignKey(Business, models.CASCADE, related_name='locations')
    name = models.CharField(max_length=256)
    address = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    is_default = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.name}"


class Product(models.Model):
    business = models.ForeignKey(Business, models.CASCADE, related_name='products')
    name = models.CharField(max_length=256)
    desc = models.TextField(null=True, blank=True)
    unit = models.ForeignKey(Unit, models.DO_NOTHING)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

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


class Supplier(models.Model):
    business = models.ForeignKey(Business, models.CASCADE, related_name='suppliers')
    name = models.CharField(max_length=256)
    business_name = models.CharField(max_length=256, null=True, blank=True)
    phone = models.CharField(max_length=256, null=True, blank=True)
    email = models.EmailField(null=True, blank=True)
    notes = models.TextField(null=True, blank=True)

    def __str__(self):
        return f"{self.business_name}: {self.name}"

