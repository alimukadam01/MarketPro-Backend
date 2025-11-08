from rest_framework import serializers
from .models import (
    City, 
    Category,
    Product,
    Supplier,
    Unit,
    Business, 
    Customer,
    Location,
)


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


class BusinessSerializer(serializers.ModelSerializer):

    class Meta:
        model = Business
        fields = ['id', 'name', 'owner', 'phone', 'logo', 'is_active']


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
            business_id = self.context['business_id'],
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
        fields =  [
            'id', 'name', 'business', 'phone', 
            'email', 'address', 'city', 'notes'
        ]


class LocationSerializer(serializers.ModelSerializer):

    business = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = Location
        fields = ['id', 'business', 'name', 'address', 'created_at']

    def create(self, validated_data):
        return Location.objects.create(
            business_id = self.context['business_id'],
            **validated_data
        )
    
    def update(self, instance, validated_data):
        return super().update(instance, validated_data)


class SimpleLocationSerializer(serializers.Serializer):

    class Meta:
        model = Location
        fields = ['id', 'name', 'address']


class ProductSerializer(serializers.ModelSerializer):

    business = SimpleBusinessSerializer(read_only=True)
    unit = UnitSerializer(read_only=True)

    class Meta:
        model = Product
        fields = [
            'id', 'business', 'name', 'desc', 
            'unit', 'created_at', 'updated_at',
        ]


class SimpleProductSerializer(serializers.ModelSerializer):

    class Meta:
        model = Product
        fields = ['id', 'name', 'unit', 'desc']


class ProductCreateUpdateSerializer(serializers.ModelSerializer):

    class Meta:
        model = Product
        fields = [
            'id', 'name', 'desc', 
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


class SupplierSerializer(serializers.ModelSerializer):

    business = SimpleBusinessSerializer(read_only=True)

    class Meta:
        model = Supplier
        fields = [
            'id', 'business' ,'name', 'business_name', 'phone', 'email', 'notes'
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
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.none())
    quantity = serializers.IntegerField()
    track_code = serializers.CharField(required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)

    def  __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        business_id = self.context.get('business_id')
        if business_id:
            self.fields['product'].queryset = Product.objects.filter(business_id=business_id)

            if self.fields.get('location'):
                self.fields['location'].queryset = Location.objects.filter(business_id=business_id)        


