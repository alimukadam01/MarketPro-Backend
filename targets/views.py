from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.filters import SearchFilter
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet, ModelViewSet

from root.utils import get_active_business

from .catalogue import catalogue_payload
from .models import ManualDataPoint, Target, TargetFilter
from .permissions import HasTargetsWriteAccess
from .serializers import (
    ManualDataPointCreateSerializer, ManualDataPointSerializer,
    ManualDataPointUpdateSerializer, SimpleManualDataPointSerializer,
    SimpleTargetSerializer, TargetCreateSerializer, TargetSerializer,
    TargetUpdateSerializer,
)
from .utils import dashboard_progress, has_targets_access, progress, summarise


NO_BUSINESS = {'detail': 'No active business exists. Please contact admin.'}
NO_ACCESS = {'detail': 'You do not have access to targets.'}


### get_queryset returns Model.objects.none() rather than the [] that the rest
### of this project returns. A plain list has no .model, and DjangoFilterBackend
### reads queryset.model on every list request, so [] turns "you do not have
### this module" into a 500. An empty queryset serialises to [] just the same.
### backlog and accounts pair [] with DjangoFilterBackend the same way and have
### the same latent fault.


class TargetViewSet(ModelViewSet):

    # IsAuthenticated is listed explicitly: permission_classes REPLACES
    # DEFAULT_PERMISSION_CLASSES rather than adding to it, so naming only the
    # targets gate here would leave these endpoints open to anonymous callers.
    permission_classes = [IsAuthenticated, HasTargetsWriteAccess]
    filter_backends = [SearchFilter, DjangoFilterBackend]
    filterset_fields = ['data_point', 'period_type',
                        'scope_type', 'subject_type', 'rule_type']
    search_fields = ['id', 'name', 'data_point', 'subject_label',
                     'outcome', 'notes']

    def get_queryset(self):
        business = get_active_business(self.request)
        if not business:
            return Target.objects.none()
        if not has_targets_access(self.request, business):
            return Target.objects.none()
        return (
            Target.objects
            .for_business(business.id)
            .prefetch_related('filters')
            .order_by('-created_at')
        )

    def get_serializer_class(self):
        method = self.request.method
        if self.action == 'list':
            return SimpleTargetSerializer
        if method == 'POST':
            return TargetCreateSerializer
        if method in ('PUT', 'PATCH'):
            return TargetUpdateSerializer
        return TargetSerializer

    def get_serializer_context(self):
        business = get_active_business(self.request)
        if not business:
            return {}
        return {'business_id': business.id, 'user_id': self.request.user.id}

    @action(['GET'], detail=False, url_path='catalogue', url_name='catalogue')
    def catalogue(self, request):
        """
        What can be measured, and what each measure permits.

        Entity options are not inlined. A shop can have thousands of customers,
        so the form fetches them from the existing list endpoints the way every
        other form does.
        """
        business = get_active_business(request)
        if not business:
            return Response(NO_BUSINESS, status=status.HTTP_400_BAD_REQUEST)
        if not has_targets_access(request, business):
            return Response(NO_ACCESS, status=status.HTTP_403_FORBIDDEN)

        try:
            return Response({'catalogue': catalogue_payload()},
                            status=status.HTTP_200_OK)
        except Exception as error:
            print(error)
            return Response({'detail': 'Internal Server Error.'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(['POST'], detail=True, url_path='duplicate', url_name='duplicate')
    def duplicate(self, request, pk=None):
        """
        Copies a target and its filters into a new one.

        Allowed on a target whose period has ended, which is the point: that is
        how next year's arrangement is set up without editing last year's record.
        """
        business = get_active_business(request)
        if not business:
            return Response(NO_BUSINESS, status=status.HTTP_400_BAD_REQUEST)
        if not has_targets_access(request, business):
            return Response(NO_ACCESS, status=status.HTTP_403_FORBIDDEN)

        source = Target.objects.filter(id=pk, business_id=business.id).first()
        if not source:
            return Response({'detail': 'Not Found'},
                            status=status.HTTP_404_NOT_FOUND)

        try:
            rows = list(source.filters.all())
            copy = Target.objects.create(
                business_id=business.id,
                created_by_id=request.user.id,
                name=f'{source.name} (copy)',
                data_point_kind=source.data_point_kind,
                data_point=source.data_point,
                scope_type=source.scope_type,
                subject_type=source.subject_type,
                subject_id=source.subject_id,
                subject_label=source.subject_label,
                period_type=source.period_type,
                period_year=source.period_year,
                period_month=source.period_month,
                period_quarter=source.period_quarter,
                date_from=source.date_from,
                date_to=source.date_to,
                rolling_days=source.rolling_days,
                target_value=source.target_value,
                rule_type=source.rule_type,
                outcome=source.outcome,
                notes=source.notes,
            )
            if rows:
                TargetFilter.objects.bulk_create([
                    TargetFilter(
                        target=copy, dimension=row.dimension,
                        value_id=row.value_id, value_text=row.value_text,
                        label=row.label,
                    ) for row in rows
                ])

            return Response(TargetSerializer(copy).data,
                            status=status.HTTP_201_CREATED)
        except Exception as error:
            print(error)
            return Response({'detail': 'Internal Server Error.'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(['POST'], detail=False, url_path='bulk-delete',
            url_name='bulk-delete')
    def bulk_delete(self, request):
        business = get_active_business(request)
        if not business:
            return Response(NO_BUSINESS, status=status.HTTP_400_BAD_REQUEST)
        if not has_targets_access(request, business):
            return Response(NO_ACCESS, status=status.HTTP_403_FORBIDDEN)

        target_ids = request.data.get('target_ids', [])
        if not target_ids:
            return Response({'detail': 'Bad Request.'},
                            status=status.HTTP_400_BAD_REQUEST)

        try:
            # Scoped to the business as well as the ids: an id belonging to
            # somebody else must not be deletable by guessing it.
            Target.objects.filter(
                id__in=target_ids, business_id=business.id).delete()
            return Response({'detail': 'Success.'}, status=status.HTTP_200_OK)
        except Exception as error:
            print(error)
            return Response({'detail': 'Internal Server Error.'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class ManualDataPointViewSet(ModelViewSet):

    # IsAuthenticated is listed explicitly: permission_classes REPLACES
    # DEFAULT_PERMISSION_CLASSES rather than adding to it, so naming only the
    # targets gate here would leave these endpoints open to anonymous callers.
    permission_classes = [IsAuthenticated, HasTargetsWriteAccess]
    filter_backends = [SearchFilter, DjangoFilterBackend]
    filterset_fields = ['value_type']
    search_fields = ['id', 'name', 'notes']

    def get_queryset(self):
        business = get_active_business(self.request)
        if not business:
            return ManualDataPoint.objects.none()
        if not has_targets_access(self.request, business):
            return ManualDataPoint.objects.none()
        return (
            ManualDataPoint.objects
            .for_business(business.id)
            .order_by('-created_at')
        )

    def get_serializer_class(self):
        method = self.request.method
        if self.action == 'list':
            return SimpleManualDataPointSerializer
        if method == 'POST':
            return ManualDataPointCreateSerializer
        if method in ('PUT', 'PATCH'):
            return ManualDataPointUpdateSerializer
        return ManualDataPointSerializer

    def get_serializer_context(self):
        business = get_active_business(self.request)
        if not business:
            return {}
        return {'business_id': business.id, 'user_id': self.request.user.id}

    @action(['POST'], detail=False, url_path='bulk-delete',
            url_name='bulk-delete')
    def bulk_delete(self, request):
        business = get_active_business(request)
        if not business:
            return Response(NO_BUSINESS, status=status.HTTP_400_BAD_REQUEST)
        if not has_targets_access(request, business):
            return Response(NO_ACCESS, status=status.HTTP_403_FORBIDDEN)

        data_point_ids = request.data.get('data_point_ids', [])
        if not data_point_ids:
            return Response({'detail': 'Bad Request.'},
                            status=status.HTTP_400_BAD_REQUEST)

        try:
            ManualDataPoint.objects.filter(
                id__in=data_point_ids, business_id=business.id).delete()
            return Response({'detail': 'Success.'}, status=status.HTTP_200_OK)
        except Exception as error:
            print(error)
            return Response({'detail': 'Internal Server Error.'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class TargetKPIViewSet(GenericViewSet):
    """
    Read-only progress. Nothing here is stored; every figure is computed when
    asked for, so these endpoints are safe to call as often as the page needs.
    """

    # IsAuthenticated is listed explicitly: permission_classes REPLACES
    # DEFAULT_PERMISSION_CLASSES rather than adding to it, so naming only the
    # targets gate here would leave these endpoints open to anonymous callers.
    permission_classes = [IsAuthenticated, HasTargetsWriteAccess]
    queryset = []
    serializer_class = None

    def _guard(self, request):
        """
        Returns (business, error_response). get_active_business costs a query or
        three, so each action calls this once rather than reaching for it again.
        """
        business = get_active_business(request)
        if not business:
            return None, Response(NO_BUSINESS,
                                  status=status.HTTP_400_BAD_REQUEST)
        if not has_targets_access(request, business):
            return None, Response(NO_ACCESS, status=status.HTTP_403_FORBIDDEN)
        return business, None

    def _targets(self, business, request):
        queryset = (Target.objects
                    .for_business(business.id)
                    .prefetch_related('filters')
                    .order_by('-created_at'))

        data_point = request.query_params.get('data_point')
        if data_point:
            queryset = queryset.filter(data_point=data_point)

        period_type = request.query_params.get('period_type')
        if period_type:
            queryset = queryset.filter(period_type=period_type)

        return list(queryset)

    @action(['GET'], detail=False, url_path='dashboard', url_name='dashboard')
    def dashboard(self, request):
        business, error = self._guard(request)
        if error:
            return error

        try:
            rows = dashboard_progress(self._targets(business, request))

            wanted = request.query_params.get('status')
            if wanted:
                rows = [row for row in rows if row['status'] == wanted]

            return Response({'dashboard': rows}, status=status.HTTP_200_OK)
        except Exception as error:
            print(error)
            return Response({'detail': 'Internal Server Error.'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(['GET'], detail=False, url_path='summary', url_name='summary')
    def summary(self, request):
        business, error = self._guard(request)
        if error:
            return error

        try:
            rows = dashboard_progress(self._targets(business, request))
            return Response({'summary': summarise(rows)},
                            status=status.HTTP_200_OK)
        except Exception as error:
            print(error)
            return Response({'detail': 'Internal Server Error.'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(['GET'], detail=False, url_path='progress', url_name='progress')
    def progress(self, request):
        business, error = self._guard(request)
        if error:
            return error

        target_id = request.query_params.get('target_id')
        if not target_id:
            return Response({'detail': 'Bad Request.'},
                            status=status.HTTP_400_BAD_REQUEST)

        target = (Target.objects
                  .filter(id=target_id, business_id=business.id)
                  .prefetch_related('filters')
                  .first())
        if not target:
            return Response({'detail': 'Not Found'},
                            status=status.HTTP_404_NOT_FOUND)

        try:
            return Response({'progress': progress(target)},
                            status=status.HTTP_200_OK)
        except Exception as error:
            print(error)
            return Response({'detail': 'Internal Server Error.'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
