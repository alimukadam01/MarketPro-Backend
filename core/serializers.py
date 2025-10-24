from rest_framework import serializers

class UserSerializer(serializers.Serializer):

    id = serializers.IntegerField(read_only=True)
    first_name = serializers.CharField()
    last_name = serializers.CharField()
    email = serializers.EmailField()
    is_superuser = serializers.BooleanField(read_only=True)

    def get_name(self, user):
        return f'{str(user.first_name)} {str(user.last_name)}'
    
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