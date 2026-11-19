"""
/people/: one record per person, with their roles and both sides of their
khaata (register #29). The rules live in root/people.py.
"""
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.filters import SearchFilter
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet

from .models import Person
from .people import (
    HasPeopleAccess, PeopleError, change_role, delete_person, delete_preview,
    duplicates, has_records, is_walk_in, last_activity, merge, side_balances,
)
from .serializers import (
    PersonDetailSerializer, PersonSerializer, PersonWriteSerializer,
)
from .utils import get_active_business

NO_BUSINESS = {'detail': 'No active business exists. Please contact admin.'}
SIDES = ('receivable', 'payable', 'both')


class PersonViewSet(ModelViewSet):

    permission_classes = [IsAuthenticated, HasPeopleAccess]
    filter_backends = [SearchFilter]
    search_fields = [
        'name', 'phone', 'business_name', 'email', 'city__name',
    ]

    def get_queryset(self):
        business = get_active_business(self.request)
        if not business:
            return Person.objects.none()

        queryset = (Person.objects.filter(business_id=business.id)
                    .select_related('city', 'customer', 'supplier', 'customer__opening_balance',
                                    'supplier__opening_balance')
                    .order_by('name', 'id'))

        params = self.request.query_params
        role = params.get('role')
        if role in ('customer', 'supplier'):
            queryset = queryset.filter(**{f'{role}__isnull': False})
            # ?active=false lists the people whose role is switched off.
            active = params.get('active')
            if active in ('true', 'false'):
                queryset = queryset.filter(**{f'{role}__is_active': active == 'true'})
        elif role == 'none':
            queryset = queryset.filter(customer__isnull=True, supplier__isnull=True)
        return queryset

    def get_serializer_class(self):
        if self.action == 'list':
            return PersonSerializer
        if self.request.method in ('POST', 'PUT', 'PATCH') and self.action in (
                'create', 'update', 'partial_update'):
            return PersonWriteSerializer
        return PersonDetailSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        business = get_active_business(self.request)
        if business:
            context['business_id'] = business.id
        return context

    def _with_money(self, business, people):
        """The balances and last activity the serializer reads, for `people`."""
        ids = [person.id for person in people]
        context = self.get_serializer_context()
        context['balances'] = side_balances(business.id, ids)
        context['last_activity'] = last_activity(business.id, ids)
        return context

    def list(self, request, *args, **kwargs):
        business = get_active_business(request)
        if not business:
            return Response(NO_BUSINESS, status=status.HTTP_400_BAD_REQUEST)

        people = list(self.filter_queryset(self.get_queryset()))
        context = self._with_money(business, people)
        side = request.query_params.get('side')
        if side in SIDES:
            balances = context['balances']

            def on(person, which):
                return balances.get(person.id, {}).get(which, 0) > 0

            if side == 'both':
                people = [p for p in people if on(p, 'receivable') and on(p, 'payable')]
            else:
                people = [p for p in people if on(p, side)]
        return Response(PersonSerializer(people, many=True, context=context).data,
                        status=status.HTTP_200_OK)

    def _detail(self, business, person, code=status.HTTP_200_OK):
        context = self._with_money(business, [person])
        return Response(PersonDetailSerializer(person, context=context).data, status=code)

    def retrieve(self, request, *args, **kwargs):
        business = get_active_business(request)
        if not business:
            return Response(NO_BUSINESS, status=status.HTTP_400_BAD_REQUEST)
        return self._detail(business, self.get_object())

    def create(self, request, *args, **kwargs):
        business = get_active_business(request)
        if not business:
            return Response(NO_BUSINESS, status=status.HTTP_400_BAD_REQUEST)
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        person = serializer.save()
        return self._detail(business, self.get_queryset().get(pk=person.pk), status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        business = get_active_business(request)
        if not business:
            return Response(NO_BUSINESS, status=status.HTTP_400_BAD_REQUEST)
        person = self.get_object()
        serializer = self.get_serializer(person, data=request.data,
                                         partial=kwargs.pop('partial', False))
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return self._detail(business, self.get_queryset().get(pk=person.pk))

    # ── Roles ────────────────────────────────────────────────────────────────

    @action(['POST'], detail=True, url_path='roles', url_name='roles')
    def roles(self, request, pk=None):
        """{"role": "customer"|"supplier", "action": "add"|"activate"|"deactivate"}"""
        business = get_active_business(request)
        if not business:
            return Response(NO_BUSINESS, status=status.HTTP_400_BAD_REQUEST)
        person = self.get_object()
        try:
            change_role(person, request.data.get('role'), request.data.get('action'))
        except PeopleError as error:
            return Response({'detail': str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return self._detail(business, self.get_queryset().get(pk=person.pk))

    # ── Delete ───────────────────────────────────────────────────────────────

    @action(['GET'], detail=True, url_path='delete-preview', url_name='delete-preview')
    def delete_preview(self, request, pk=None):
        """Everything deleting this person would remove, and what it keeps."""
        if not get_active_business(request):
            return Response(NO_BUSINESS, status=status.HTTP_400_BAD_REQUEST)
        try:
            return Response(delete_preview(self.get_object()), status=status.HTTP_200_OK)
        except Exception as error:
            print(error)
            return Response({'detail': 'Internal Server Error.'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def destroy(self, request, *args, **kwargs):
        """Needs {"confirm_name": "<the person's name>"} (register R12)."""
        if not get_active_business(request):
            return Response(NO_BUSINESS, status=status.HTTP_400_BAD_REQUEST)
        person = self.get_object()
        confirm = request.data.get('confirm_name') or request.query_params.get('confirm_name')
        try:
            delete_person(person, confirm)
        except PeopleError as error:
            return Response({'detail': str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'detail': 'Success.'}, status=status.HTTP_200_OK)

    @action(['POST'], detail=False, url_path='bulk-delete', url_name='bulk-delete')
    def bulk_delete(self, request):
        """
        Only people with nothing on record go together; anyone with invoices,
        payments or balances is deleted one at a time, after the preview.
        """
        if not get_active_business(request):
            return Response(NO_BUSINESS, status=status.HTTP_400_BAD_REQUEST)
        ids = request.data.get('person_ids', [])
        if not ids:
            return Response({'detail': 'Bad Request.'}, status=status.HTTP_400_BAD_REQUEST)
        people = list(self.get_queryset().filter(id__in=ids))
        refused = [person.name for person in people if is_walk_in(person) or has_records(person)]
        if refused:
            return Response({
                'detail': 'These people have records and must be deleted one at a time, '
                          'after seeing what goes with them: ' + ', '.join(refused) + '.',
                'refused': refused,
            }, status=status.HTTP_400_BAD_REQUEST)
        for person in people:
            person.delete()
        return Response({'detail': 'Success.'}, status=status.HTTP_200_OK)

    # ── Duplicates and merge ─────────────────────────────────────────────────

    @action(['GET'], detail=False, url_path='duplicates', url_name='duplicates')
    def duplicates(self, request):
        """People who share a phone number: likely the same person (#17)."""
        business = get_active_business(request)
        if not business:
            return Response(NO_BUSINESS, status=status.HTTP_400_BAD_REQUEST)
        groups = duplicates(business.id)
        everyone = [person for group in groups for person in group['people']]
        context = self._with_money(business, everyone)
        return Response([
            {'phone': group['phone'],
             'people': PersonSerializer(group['people'], many=True, context=context).data}
            for group in groups
        ], status=status.HTTP_200_OK)

    @action(['POST'], detail=True, url_path='merge', url_name='merge')
    def merge(self, request, pk=None):
        """{"other_id": id, "name": "...", "phone": "..."}: folds other into this one."""
        business = get_active_business(request)
        if not business:
            return Response(NO_BUSINESS, status=status.HTTP_400_BAD_REQUEST)
        kept = self.get_object()
        other = self.get_queryset().filter(pk=request.data.get('other_id')).first()
        if other is None:
            return Response({'detail': 'Not Found'}, status=status.HTTP_404_NOT_FOUND)
        try:
            merge(kept, other, request.data.get('name'), request.data.get('phone'))
        except PeopleError as error:
            return Response({'detail': str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return self._detail(business, self.get_queryset().get(pk=kept.pk))
