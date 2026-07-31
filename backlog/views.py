from rest_framework import status
from rest_framework.decorators import action
from rest_framework.filters import SearchFilter
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet
from django_filters.rest_framework import DjangoFilterBackend

from root.utils import get_active_business
from .models import BacklogEntry
from .serializers import (
    BacklogEntryCreateSerializer,
    BacklogEntrySerializer,
    BacklogEntryUpdateSerializer,
    SimpleBacklogEntrySerializer,
)


class BacklogEntryViewSet(ModelViewSet):

    filter_backends = [SearchFilter, DjangoFilterBackend]
    filterset_fields = ['type', 'is_done', 'assigned_to']
    search_fields = ['id', 'type', 'notes', 'assigned_to__user__email']

    def get_queryset(self):
        business = get_active_business(self.request)
        if not business:
            return []

        user = self.request.user
        if user.role == 'admin':
            return BacklogEntry.objects.filter(
                business_id=business.id
            ).order_by('-created_at')

        return BacklogEntry.objects.for_employee(
            business.id, user.id
        ).order_by('-created_at')

    def get_serializer_class(self):
        method = self.request.method
        if self.action == 'list':
            return SimpleBacklogEntrySerializer
        if method == 'POST':
            return BacklogEntryCreateSerializer
        if method in ('PUT', 'PATCH'):
            return BacklogEntryUpdateSerializer
        return BacklogEntrySerializer

    def get_serializer_context(self):
        business = get_active_business(self.request)
        if not business:
            return {}
        return {
            'business_id': business.id,
            'user_id': self.request.user.id,
        }

    def destroy(self, request, *args, **kwargs):
        if request.user.role != 'admin':
            return Response(
                {'detail': 'Only admins can delete backlog entries.'},
                status=status.HTTP_403_FORBIDDEN
            )
        try:
            instance = self.get_object()
            instance.delete()
            return Response({'detail': 'Success.'}, status=status.HTTP_200_OK)
        except Exception as error:
            print(error)
            return Response(
                {'detail': 'Internal Server Error.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(['POST'], detail=True, url_path='toggle-status', url_name='toggle-status')
    def toggle_status(self, request, pk=None):
        business = get_active_business(request)
        if not business:
            return Response(
                {'detail': 'No active business exists. Please contact admin.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        try:
            instance = self.get_object()
            instance.is_done = not instance.is_done
            instance.save(update_fields=['is_done'])
            return Response(
                {'detail': 'OK', 'is_done': instance.is_done},
                status=status.HTTP_200_OK
            )
        except Exception as error:
            print(error)
            return Response(
                {'detail': 'Internal Server Error.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(['POST'], detail=False, url_path='bulk-delete', url_name='bulk-delete')
    def bulk_delete(self, request):
        if request.user.role != 'admin':
            return Response(
                {'detail': 'Only admins can delete backlog entries.'},
                status=status.HTTP_403_FORBIDDEN
            )

        entry_ids = request.data.get('entry_ids', [])
        if not entry_ids:
            return Response(
                {'detail': 'Bad Request.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            BacklogEntry.objects.filter(id__in=entry_ids).delete()
            return Response({'detail': 'Success.'}, status=status.HTTP_200_OK)
        except Exception as error:
            print(error)
            return Response(
                {'detail': 'Internal Server Error.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
