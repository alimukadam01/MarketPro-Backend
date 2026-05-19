from rest_framework import serializers
from django.db import transaction
from django.contrib.auth import get_user_model
from .models import (
    City,
    Category,
    Expense,
    Product,
    ProductVariantType,
    ProductVariant,
    Supplier,
    Unit,
    Business,
    Customer,
    Location,
    Employee,
    EmployeeAccess,
    BusinessConfig,
)
from .utils import generate_sku, generate_variant_name

User = get_user_model()


class CitySerializer(serializers.ModelSerializer):

    class Meta:
        model = City
        fields = ['id', 'name', 'postal_code']


class CategorySerializer(serializers.ModelSerializer):

    class Meta:
        model = Category
        fields = ['id', 'name', 'desc']


class UnitSerializer(serializers.ModelSerializer):

    class Meta:
        model = Unit
        fields = ['id', 'name', 'abv']


# ── BusinessConfig ────────────────────────────────────────────────────────────

class BusinessConfigSerializer(serializers.ModelSerializer):

    class Meta:
        model = BusinessConfig
        fields = [
            'id', 'business',
            'sales', 'purchases', 'projects',
            'inventory', 'returned_items', 'quotations',
        ]


class SimpleBusinessConfigSerializer(serializers.ModelSerializer):

    class Meta:
        model = BusinessConfig
        fields = [
            'sales', 'purchases', 'projects',
            'inventory', 'returned_items', 'quotations',
        ]


class BusinessConfigCreateSerializer(serializers.ModelSerializer):

    class Meta:
        model = BusinessConfig
        fields = [
            'sales', 'purchases', 'projects',
            'inventory', 'returned_items', 'quotations',
        ]

    def create(self, validated_data):
        return BusinessConfig.objects.create(
            business_id=self.context['business_id'],
            **validated_data,
        )


class BusinessConfigUpdateSerializer(serializers.ModelSerializer):

    class Meta:
        model = BusinessConfig
        fields = [
            'sales', 'purchases', 'projects',
            'inventory', 'returned_items', 'quotations',
        ]

    def update(self, instance, validated_data):
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        return instance


class BusinessSerializer(serializers.ModelSerializer):
    config = SimpleBusinessConfigSerializer()
    class Meta:
        model = Business
        fields = [
            'id', 'name', 'owner', 'phone',
            'address', 'logo', 'is_active', 'config'
        ]


class BusinessCreateSerializer(serializers.ModelSerializer):

    class Meta:
        model = Business
        fields = ['name', 'phone', 'logo']

    def save(self, **kwargs):
        return Business.objects.create(owner_id=self.context['owner_id'], **self.validated_data)


class SimpleBusinessSerializer(serializers.ModelSerializer):

    class Meta:
        model = Business
        fields = ['id', 'name', 'logo']


class CustomerSerializer(serializers.ModelSerializer):

    business = SimpleBusinessSerializer(read_only=True)
    total_sales = serializers.FloatField(read_only=True)

    class Meta:
        model = Customer
        fields = [
            'id', 'name', 'business', 'phone', 'email',
            'address', 'city', 'created_at', 'updated_at',
            'notes', 'total_sales'
        ]

    def create(self, validated_data):
        return Customer.objects.create(
            business_id=self.context['business_id'],
            **self.validated_data
        )

    def update(self, instance, validated_data):

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        instance.save()
        return instance


class SimpleCustomerSerializer(serializers.ModelSerializer):

    city = CitySerializer()

    class Meta:
        model = Customer
        fields = [
            'id', 'name', 'phone',
            'email', 'address', 'city'
        ]


class LocationSerializer(serializers.ModelSerializer):

    business = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = Location
        fields = ['id', 'business', 'name', 'address', 'created_at']

    def create(self, validated_data):
        return Location.objects.create(
            business_id=self.context['business_id'],
            **validated_data
        )

    def update(self, instance, validated_data):
        return super().update(instance, validated_data)


class SimpleLocationSerializer(serializers.Serializer):

    class Meta:
        model = Location
        fields = ['id', 'name', 'address']


class ProductVariantTypeSerializer(serializers.ModelSerializer):

    class Meta:
        model = ProductVariantType
        fields = ['id', 'name']


class SimpleProductVariantSerializer(serializers.ModelSerializer):

    class Meta:
        model = ProductVariant
        fields = ['id', 'name', 'sku', 'attributes', 'is_active']


class ProductSerializer(serializers.ModelSerializer):

    business = SimpleBusinessSerializer(read_only=True)
    code = serializers.CharField()
    unit = UnitSerializer(read_only=True)
    variants = SimpleProductVariantSerializer(many=True)

    class Meta:
        model = Product
        fields = [
            'id', 'business', 'code', 'name', 'desc',
            'unit', 'created_at', 'updated_at', 'variants'
        ]


class SimpleProductSerializer(serializers.ModelSerializer):

    unit = UnitSerializer()

    class Meta:
        model = Product
        fields = ['id', 'name', 'code', 'unit', 'desc']


class ProductCreateUpdateSerializer(serializers.ModelSerializer):

    class Meta:
        model = Product
        fields = [
            'id', 'name', 'desc', 'code',
            'unit', 'created_at', 'updated_at',
        ]

    def create(self, validated_data):
        return Product.objects.create(
            business_id=self.context['business_id'],
            **validated_data
        )

    def update(self, instance, validated_data):
        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        instance.save()
        return instance


class ProductVariantSerializer(serializers.ModelSerializer):

    base = SimpleProductSerializer(read_only=True)

    class Meta:
        model = ProductVariant
        fields = ['id', 'base', 'name', 'sku', 'attributes', 'is_active']


class ProductVariantCreateSerializer(serializers.ModelSerializer):

    name = serializers.CharField(required=False)

    def save(self, **kwargs):

        if not self.validated_data.get("name"):
            self.validated_data['name'] = generate_variant_name(
                self.validated_data.get('attributes')
            )

        sku = generate_sku(self.context['business_id'])
        return ProductVariant.objects.create(
            base_id=self.context['product_id'],
            sku=sku,
            **self.validated_data
        )

    class Meta:
        model = ProductVariant
        fields = ['id', 'name', 'attributes']


class ProductAndVariantUpdateSerializer(serializers.ModelSerializer):

    variants = serializers.ListField()

    def save(self, **kwargs):

        variants = self.validated_data.pop('variants', [])
        instance = self.instance

        if variants:
            # separate new and old variants
            new_variants = [v for v in variants if not v.get("id")]
            existing_variants = ProductVariant.objects.filter(base=instance)
            existing_variant_ids = set([var.id for var in existing_variants])
            existing_variant_pData = [v for v in variants if v.get("id")]
            existing_variant_pIds = set([v['id'] for v in variants if v.get("id")])

            # Determine variants to be deleted
            tbd_variants = existing_variant_ids - existing_variant_pIds

            # create old variants id map
            existing_variants_map = {var.id: var for var in existing_variants}

            # update product attrs
            for attr, value in self.validated_data.items():
                setattr(instance, attr, value)

            # create new variant objects list
            tbc_variants = [ProductVariant(
                name=generate_variant_name(var['attributes']),
                sku=generate_sku(self.context['business_id']),
                base=instance,
                **var
            ) for var in new_variants]

            tbu_variants = []
            for data in existing_variant_pData:
                variant = existing_variants_map[data['id']]
                attributes = data.pop('attributes', {})
                variant.name = generate_variant_name(attributes)
                variant.attributes = attributes
                tbu_variants.append(variant)

            try:
                with transaction.atomic():
                    ProductVariant.objects.filter(id__in=tbd_variants).delete()
                    ProductVariant.objects.bulk_create(tbc_variants)
                    ProductVariant.objects.bulk_update(
                        tbu_variants, fields=['name', 'attributes']
                    )
                    instance.save()
            except Exception as error:
                print(f"Error updating objects: {error}")
                return None
        else:
            try:
                with transaction.atomic():
                    ProductVariant.objects.filter(base=instance).delete()
                    ProductVariant.objects.create(
                        base=instance,
                        sku=generate_sku(business_id=self.context['business_id']),
                        name=generate_variant_name(),
                    )
            except Exception as error:
                print(f"Error updating objects: {error}")
                return None

        return instance

    class Meta:
        model = Product
        fields = ['id', 'name', 'unit', 'desc', 'variants']


class ProductAndVariantCreateSerializer(serializers.ModelSerializer):

    variants = serializers.ListField()

    def save(self, **kwargs):

        sku_count = 0
        try:
            with transaction.atomic():
                variants = self.validated_data.pop('variants', [])
                product = Product.objects.create(
                    business_id=self.context['business_id'], **self.validated_data
                )

                if variants:
                    new_variants = []
                    for variant in variants:
                        new_variants.append(ProductVariant(
                            base=product,
                            sku=generate_sku(
                                business_id=self.context['business_id']),
                            name=generate_variant_name(variant['attributes']),
                            **variant
                        ))
                        sku_count += 1

                    ProductVariant.objects.bulk_create(new_variants)
                else:
                    ProductVariant.objects.create(
                        base=product,
                        sku=generate_sku(business_id=self.context['business_id']),
                        name=generate_variant_name(),
                    )

        except Exception as error:
            print(f"Error creating product variants: {error}")

            business = Business.objects.get(self.context['business_id'])
            business.sku_counter -= sku_count
            business.save(update_fields=['sku_counter'])

        return product

    class Meta:
        model = Product
        fields = ['id', 'name', 'desc', 'unit', 'variants']


class SupplierSerializer(serializers.ModelSerializer):

    business = SimpleBusinessSerializer(read_only=True)

    class Meta:
        model = Supplier
        fields = [
            'id', 'business', 'name', 'business_name', 'phone', 'email', 'notes'
        ]

    def create(self, validated_data):

        return Supplier.objects.create(
            business_id=self.context['business_id'],
            **self.validated_data
        )


class SimpleSupplierSerializer(serializers.ModelSerializer):

    class Meta:
        model = Supplier
        fields = [
            'id', 'name', 'business_name',
        ]


class BaseItemSerializer(serializers.Serializer):

    id = serializers.IntegerField(read_only=True)
    business = SimpleBusinessSerializer(read_only=True)
    product = serializers.PrimaryKeyRelatedField(
        queryset=ProductVariant.objects.none()
    )
    quantity = serializers.IntegerField()
    track_code = serializers.CharField(required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        business_id = self.context.get('business_id')
        if business_id:
            self.fields['product'].queryset = ProductVariant.objects.filter(
                base__business_id=business_id)

            if self.fields.get('location'):
                self.fields['location'].queryset = Location.objects.filter(
                    business_id=business_id)


class ExpenseSerializer(serializers.ModelSerializer):

    business = serializers.PrimaryKeyRelatedField(read_only=True)

    def create(self, validated_data):
        return Expense.objects.create(business_id=self.context['business_id'], **validated_data)

    class Meta:
        model = Expense
        fields = ['id', 'business', 'name', 'desc', 'amount', 'created_at']


# ── Employee ──────────────────────────────────────────────────────────────────

class SimpleUserSerializer(serializers.ModelSerializer):

    class Meta:
        model = User
        fields = ['id', 'email', 'first_name', 'last_name']


class SimpleEmployeeAccessSerializer(serializers.ModelSerializer):

    class Meta:
        model = EmployeeAccess
        fields = ['id', 'permissions']

class EmployeeSerializer(serializers.ModelSerializer):

    user = SimpleUserSerializer(read_only=True)
    business = SimpleBusinessSerializer(read_only=True)
    created_by = SimpleUserSerializer(read_only=True)
    access = SimpleEmployeeAccessSerializer()

    class Meta:
        model = Employee
        fields = ['id', 'user', 'business', 'created_by', 'created_at', 'updated_at', 'access']


class EmployeeCreateSerializer(serializers.ModelSerializer):
    
    user = SimpleUserSerializer(read_only=True)
    created_by = SimpleUserSerializer(read_only=True)

    class Meta:
        model = Employee
        fields = ['user', 'business', 'created_by', 'created_at', 'updated_at']

class EmployeeAndUserCreateSerializer(serializers.ModelSerializer):

    first_name = serializers.CharField(max_length=256)
    last_name = serializers.CharField(max_length=256)
    email = serializers.EmailField()
    password = serializers.CharField(max_length=256)
    re_password = serializers.CharField(max_length=256)
    permissions = serializers.DictField()

    class Meta:
        model = Employee
        fields = ['first_name', 'last_name', 'email', 'password', 're_password', 'permissions']

    def create(self, validated_data):

        try:
            with transaction.atomic():
                email = validated_data.pop('email')
                permissions = validated_data.pop('permissions')

                user = User.objects.create_user(
                    email,
                    password=validated_data.pop('password'),
                    first_name=validated_data.pop('first_name'),
                    last_name=validated_data.pop('last_name'),
                )

                employee = Employee.objects.create(
                    user=user,
                    business_id=self.context['business_id'],
                    created_by_id=self.context['created_by_id'],
                )

                permissions = EmployeeAccess.objects.create(
                    employee = employee,
                    permissions = permissions
                )

                return employee
        except Exception as error:
            print(error)
            return None
        

class EmployeeUpdateSerializer(serializers.ModelSerializer):

    access = SimpleEmployeeAccessSerializer()
    class Meta:
        model = Employee
        fields = ['access']

    def update(self, instance, validated_data):
        access = instance.access
        payload_access = validated_data.pop('access')

        for attr, value in payload_access.items():
            setattr(access, attr, value)
        access.save()
        instance.refresh_from_db()
        return instance


# ── EmployeeAccess ────────────────────────────────────────────────────────────

class EmployeeAccessSerializer(serializers.ModelSerializer):

    class Meta:
        model = EmployeeAccess
        fields = ['id', 'employee', 'permissions']


class EmployeeAccessCreateSerializer(serializers.ModelSerializer):

    class Meta:
        model = EmployeeAccess
        fields = ['permissions']

    def create(self, validated_data):

        employee_id=self.context['employee_id']
        print(validated_data)
        print(employee_id)

        return EmployeeAccess.objects.create(
            employee_id= employee_id,
            **validated_data
        )


class EmployeeAccessUpdateSerializer(serializers.ModelSerializer):

    class Meta:
        model = EmployeeAccess
        fields = ['permissions']

    def update(self, instance, validated_data):
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        return instance

