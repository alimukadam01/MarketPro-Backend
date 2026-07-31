from rest_framework import serializers
from root.models import Employee
from core.serializers import SimpleUserSerializer
from .models import BacklogEntry


class SimpleAssignedToSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    user = SimpleUserSerializer(read_only=True)


class SimpleBacklogEntrySerializer(serializers.ModelSerializer):
    assigned_to = SimpleAssignedToSerializer(read_only=True)
    created_by = SimpleUserSerializer(read_only=True)

    class Meta:
        model = BacklogEntry
        fields = [
            'id', 'type', 'image', 'assigned_to', 'created_by',
            'is_done', 'notes', 'created_at',
        ]


class BacklogEntrySerializer(serializers.ModelSerializer):
    assigned_to = SimpleAssignedToSerializer(read_only=True)
    created_by = SimpleUserSerializer(read_only=True)

    class Meta:
        model = BacklogEntry
        fields = [
            'id', 'business', 'type', 'image', 'assigned_to', 'created_by',
            'is_done', 'notes', 'created_at', 'updated_at',
        ]


class BacklogEntryCreateSerializer(serializers.ModelSerializer):
    assigned_to = serializers.PrimaryKeyRelatedField(
        queryset=Employee.objects.none(),
        required=False,
        allow_null=True,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        business_id = self.context.get('business_id')
        if business_id:
            self.fields['assigned_to'].queryset = Employee.objects.filter(
                business_id=business_id
            )

    class Meta:
        model = BacklogEntry
        fields = ['type', 'image', 'assigned_to', 'notes']

    def save(self, **kwargs):
        return BacklogEntry.objects.create(
            business_id=self.context['business_id'],
            created_by_id=self.context['user_id'],
            **self.validated_data
        )


class BacklogEntryUpdateSerializer(serializers.ModelSerializer):
    assigned_to = serializers.PrimaryKeyRelatedField(
        queryset=Employee.objects.none(),
        required=False,
        allow_null=True,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        business_id = self.context.get('business_id')
        if business_id:
            self.fields['assigned_to'].queryset = Employee.objects.filter(
                business_id=business_id
            )

    class Meta:
        model = BacklogEntry
        fields = ['type', 'image', 'assigned_to', 'notes', 'is_done']
        extra_kwargs = {'image': {'required': False}}

    def update(self, instance, validated_data):
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        return instance
