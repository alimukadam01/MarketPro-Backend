from django.contrib import admin
from .models import (
    City, Category, Customer, Location, 
    Product, Supplier, Unit, Business,
    Expense
)

admin.site.register(City)
admin.site.register(Category)
admin.site.register(Unit)
admin.site.register(Business)
admin.site.register(Customer)
admin.site.register(Location)
admin.site.register(Product)
admin.site.register(Supplier)
admin.site.register(Expense)

