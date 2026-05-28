from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.forms.models import model_to_dict

from root.models import EmployeeAccess

User = get_user_model()

class UserSerializer(serializers.Serializer):

    id = serializers.IntegerField(read_only=True)
    first_name = serializers.CharField()
    last_name = serializers.CharField()
    email = serializers.EmailField()
    role = serializers.CharField(read_only=True)
    is_superuser = serializers.BooleanField(read_only=True)
    permissions = serializers.SerializerMethodField()

    def get_name(self, user):
        return f'{str(user.first_name)} {str(user.last_name)}'
    
    def get_permissions(self, user):

        if user.role == "employee":
            employee = user.emp_records.all()
            if employee:
                employee = employee[0]

            return employee.access.permissions
        
        if user.role == "admin":
            businesses = user.businesses.all()
            if businesses:
                business = businesses[0]

            config = model_to_dict(business.config)
            permissions = {
                attr: {
                    "view": value, "create": value, 
                    "edit": value, "delete": value
                } for attr, value in config.items() if attr not in ("business", "id", "_state")
            }

            for module in EmployeeAccess.all_modules:
                if not permissions.get(module, None):
                    permissions[module] = {
                    "view": True, "create": True, 
                    "edit": True, "delete": True
                }
        
            return permissions
        
        return []



    def update(self, instance, validated_data):
        
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        return instance


class SimpleUserSerializer(serializers.Serializer):

    id = serializers.IntegerField()
    name = serializers.SerializerMethodField()
    email = serializers.EmailField()

    def get_name(self, user):
        return f'{str(user.first_name)} {str(user.last_name)}'