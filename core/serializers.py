from rest_framework import serializers

class UserSerializer(serializers.Serializer):

    id = serializers.IntegerField()
    name = serializers.SerializerMethodField()
    email = serializers.EmailField()
    is_superuser = serializers.BooleanField()

    def get_name(self, user):
        return f'{str(user.first_name)} {str(user.last_name)}'
    

class SimpleUserSerializer(serializers.Serializer):

    id = serializers.IntegerField()
    name = serializers.SerializerMethodField()
    email = serializers.EmailField()

    def get_name(self, user):
        return f'{str(user.first_name)} {str(user.last_name)}'