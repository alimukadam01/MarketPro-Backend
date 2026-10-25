from django_filters import rest_framework as filters
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import serializers
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import ReadOnlyModelViewSet

from root.utils import get_active_business
from .models import AuditEvent


class IsBusinessOwner(BasePermission):
    """The audit trail is the owner's: it shows everyone's writes."""

    def has_permission(self, request, view):
        business = get_active_business(request)
        return bool(business and business.owner_id == request.user.id)


class AuditEventSerializer(serializers.ModelSerializer):
    actor = serializers.SerializerMethodField()

    def get_actor(self, event):
        user = event.actor
        if not user:
            return None
        return {'id': user.id, 'name': f'{user.first_name} {user.last_name}'.strip() or user.email}

    class Meta:
        model = AuditEvent
        fields = ['id', 'actor', 'source', 'model', 'object_id', 'action',
                  'changes', 'request_id', 'created_at']


class AuditEventFilter(filters.FilterSet):
    created_from = filters.DateFilter(field_name='created_at', lookup_expr='date__gte')
    created_to = filters.DateFilter(field_name='created_at', lookup_expr='date__lte')

    class Meta:
        model = AuditEvent
        fields = ['model', 'object_id', 'actor', 'action', 'source']


class AuditEventViewSet(ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated, IsBusinessOwner]
    serializer_class = AuditEventSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_class = AuditEventFilter

    # The log grows with every save; a list is capped and meant to be
    # narrowed with the filters (a model and object, an actor, a date range).
    LIST_LIMIT = 500

    def get_queryset(self):
        business = get_active_business(self.request)
        if not business:
            return AuditEvent.objects.none()
        return AuditEvent.objects.filter(business_id=business.id).select_related('actor')

    def list(self, request, *args, **kwargs):
        rows = self.filter_queryset(self.get_queryset())[:self.LIST_LIMIT]
        return Response(self.get_serializer(rows, many=True).data)
