from django.contrib import admin
from .models import (
    City, Category, Customer, Location, 
    Product, Supplier, Unit, Business,
    Expense, ProductVariantType, 
    ProductVariant, Employee, EmployeeAccess, BusinessConfig
)

admin.site.register(City)
admin.site.register(Category)
admin.site.register(Unit)
admin.site.register(Business)
admin.site.register(BusinessConfig)
admin.site.register(Employee)
admin.site.register(EmployeeAccess)
admin.site.register(Customer)
admin.site.register(Location)
admin.site.register(Product)
admin.site.register(ProductVariant)
admin.site.register(ProductVariantType)
admin.site.register(Supplier)
admin.site.register(Expense)

