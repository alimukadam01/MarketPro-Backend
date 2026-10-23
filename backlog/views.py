"""
The owner's side of the backlog: uploads, the list, review, the glossary and
the figures. The entry person's side is backlog/work_views.py.
"""
from django.db import transaction
from django.core.files.base import ContentFile
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import mixins, status
from rest_framework.decorators import action
from rest_framework.filters import SearchFilter
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from root.utils import get_active_business
from sales.models import InvoiceImage
from .access import BacklogModulePermission, IsBusinessOwner, is_owner
from .glossary import approve_suggestion, change_term, current_version, reject_suggestion, add_term, remove_term
from .metrics import business_metrics, person_metrics
from .models import (
    BacklogEntry, BacklogReview, GlossaryEntry, GlossarySuggestion, UploadBatch,
)
from .serializers import (
    BacklogEntrySerializer, BacklogEntryUpdateSerializer, GlossaryEntrySerializer,
    GlossaryEntryWriteSerializer, GlossarySuggestionSerializer, UploadBatchCreateSerializer,
    UploadBatchSerializer,
)
from .uploads import UploadTooLarge, extract_images

NO_BUSINESS = {'detail': 'No active business exists. Please contact admin.'}
OWNER_ONLY = {'detail': 'Only the owner can do that.'}


def _entries(request):
    business = get_active_business(request)
    if not business:
        return BacklogEntry.objects.none()
    return (BacklogEntry.objects.filter(business_id=business.id)
            .select_related('claimed_by', 'entered_by', 'verified_by')
            .prefetch_related('outputs__sales_invoice__customer',
                              'outputs__purchase_invoice__supplier',
                              'outputs__product__unit',
                              'outputs__inventory_item__product__base',
                              'outputs__inventory_item__location',
                              'new_records__customer__city', 'new_records__customer__opening_balance',
                              'new_records__supplier__opening_balance', 'new_records__product__unit',
                              'new_records__product__variants'))


class UploadBatchViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, GenericViewSet):
    """POST /backlog-batches/ - photos, a folder's files or a zip, as
    `files`. Each image becomes one pending entry."""

    permission_classes = [IsAuthenticated, BacklogModulePermission]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    serializer_class = UploadBatchSerializer

    def get_queryset(self):
        business = get_active_business(self.request)
        if not business:
            return UploadBatch.objects.none()
        return (UploadBatch.objects.filter(business_id=business.id)
                .select_related('uploaded_by').order_by('-created_at'))

    def create(self, request):
        business = get_active_business(request)
        if not business:
            return Response(NO_BUSINESS, status=status.HTTP_400_BAD_REQUEST)
        meta = UploadBatchCreateSerializer(data=request.data)
        meta.is_valid(raise_exception=True)
        files = request.FILES.getlist('files')
        if not files:
            return Response({'detail': 'Choose some photos, a folder or a zip to upload.'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            images, skipped = extract_images(files)
        except UploadTooLarge as error:
            return Response({'detail': str(error)}, status=status.HTTP_400_BAD_REQUEST)
        if not images:
            return Response({'detail': 'None of the files were images.', 'skipped': skipped},
                            status=status.HTTP_400_BAD_REQUEST)

        values = meta.validated_data
        with transaction.atomic():
            batch = UploadBatch.objects.create(
                business=business, uploaded_by=request.user, skipped_files=skipped, **values)
            for name, data in images:
                BacklogEntry.objects.create(
                    business=business, batch=batch, type=values['type'],
                    book_label=values['book_label'], priority=values['priority'],
                    is_daily=values['is_daily'], notes=values['notes'] or None,
                    created_by=request.user, image=ContentFile(data, name=name))
        return Response({**UploadBatchSerializer(batch).data, 'pages': len(images), 'skipped': skipped},
                        status=status.HTTP_201_CREATED)


class BacklogEntryViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin,
                          mixins.UpdateModelMixin, mixins.DestroyModelMixin, GenericViewSet):
    """/backlog-entries/ - the owner's list of pages, and reviewing them."""

    permission_classes = [IsAuthenticated, BacklogModulePermission]
    filter_backends = [SearchFilter, DjangoFilterBackend]
    filterset_fields = ['type', 'status', 'priority', 'is_daily', 'batch', 'entered_by']
    search_fields = ['id', 'book_label', 'notes', 'review_note']

    def get_queryset(self):
        return _entries(self.request).order_by('-created_at', '-id')

    def get_serializer_class(self):
        if self.request.method in ('PUT', 'PATCH'):
            return BacklogEntryUpdateSerializer
        return BacklogEntrySerializer

    def perform_destroy(self, entry):
        if entry.image:
            entry.image.delete(save=False)
        entry.delete()

    @action(['POST'], detail=False, url_path='bulk-delete', url_name='bulk-delete')
    def bulk_delete(self, request):
        ids = request.data.get('entry_ids', [])
        if not ids:
            return Response({'detail': 'Bad Request.'}, status=status.HTTP_400_BAD_REQUEST)
        for entry in self.get_queryset().filter(id__in=ids):
            self.perform_destroy(entry)
        return Response({'detail': 'Success.'}, status=status.HTTP_200_OK)

    def _owner_entry(self, request, pk, statuses):
        business = get_active_business(request)
        if not is_owner(request.user, business):
            return None, Response(OWNER_ONLY, status=status.HTTP_403_FORBIDDEN)
        entry = self.get_queryset().filter(pk=pk).first()
        if not entry:
            return None, Response({'detail': 'Not Found'}, status=status.HTTP_404_NOT_FOUND)
        if entry.status not in statuses:
            return None, Response({'detail': f'This page is {entry.get_status_display().lower()}.'},
                                  status=status.HTTP_409_CONFLICT)
        return entry, None

    @action(['POST'], detail=True, url_path='release', url_name='release')
    def release(self, request, pk=None):
        """Take a page off whoever has it and put it back in the pool."""
        entry, error = self._owner_entry(request, pk, [BacklogEntry.IN_PROGRESS])
        if error:
            return error
        entry.status = BacklogEntry.PENDING
        entry.claimed_by = None
        entry.claimed_at = None
        entry.save(update_fields=['status', 'claimed_by', 'claimed_at', 'updated_at'])
        return Response(BacklogEntrySerializer(entry, context={'request': request}).data)

    @action(['GET'], detail=False, url_path='review-queue', url_name='review-queue')
    def review_queue(self, request):
        """Everything waiting for the owner, oldest first: entered pages,
        then the ones the entry person could not read."""
        business = get_active_business(request)
        if not is_owner(request.user, business):
            return Response(OWNER_ONLY, status=status.HTTP_403_FORBIDDEN)
        entered = list(self.get_queryset().filter(status=BacklogEntry.ENTERED).order_by('entered_at', 'id'))
        unreadable = list(self.get_queryset().filter(status=BacklogEntry.CANT_READ).order_by('entered_at', 'id'))
        serializer = BacklogEntrySerializer(entered + unreadable, many=True, context={'request': request})
        return Response(serializer.data)

    @action(['POST'], detail=True, url_path='approve', url_name='approve')
    def approve(self, request, pk=None):
        """Verified. An invoice's photo moves onto the invoice; a list's
        photo is deleted (§9). The entry row stays as the record."""
        entry, error = self._owner_entry(request, pk, [BacklogEntry.ENTERED])
        if error:
            return error
        with transaction.atomic():
            if entry.image:
                invoice_outputs = [o for o in entry.outputs.all()
                                   if o.kind in ('sales_invoice', 'purchase_invoice')]
                if invoice_outputs:
                    output = invoice_outputs[0]
                    InvoiceImage.objects.create(
                        **{output.kind: output.record}, image=entry.image.name,
                        caption=f'Backlog page #{entry.id}', uploaded_by=request.user)
                else:
                    entry.image.delete(save=False)
                entry.image = None
            BacklogReview.objects.create(entry=entry, reviewer=request.user,
                                         entered_by=entry.entered_by, outcome=BacklogReview.APPROVED)
            entry.status = BacklogEntry.VERIFIED
            entry.verified_by = request.user
            entry.verified_at = timezone.now()
            entry.review_note = ''
            entry.save()
        return Response(BacklogEntrySerializer(entry, context={'request': request}).data)

    @action(['POST'], detail=True, url_path='send-back', url_name='send-back')
    def send_back(self, request, pk=None):
        """Back to the top of the entry queue with the owner's reason. Its
        records stay, for whoever picks it up to correct."""
        reason = (request.data.get('reason') or '').strip()
        if not reason:
            return Response({'reason': ['Say what is wrong, so it can be fixed.']},
                            status=status.HTTP_400_BAD_REQUEST)
        entry, error = self._owner_entry(request, pk, [BacklogEntry.ENTERED])
        if error:
            return error
        with transaction.atomic():
            BacklogReview.objects.create(entry=entry, reviewer=request.user, entered_by=entry.entered_by,
                                         outcome=BacklogReview.SENT_BACK, note=reason)
            entry.status = BacklogEntry.PENDING
            entry.review_note = reason
            entry.rejection_count += 1
            entry.claimed_by = None
            entry.claimed_at = None
            entry.save()
        return Response(BacklogEntrySerializer(entry, context={'request': request}).data)

    @action(['POST'], detail=True, url_path='requeue', url_name='requeue',
            parser_classes=[MultiPartParser, FormParser, JSONParser])
    def requeue(self, request, pk=None):
        """A page nobody could read goes back to the pool - with a better
        photo, when one is sent as `image`."""
        entry, error = self._owner_entry(request, pk, [BacklogEntry.CANT_READ])
        if error:
            return error
        image = request.FILES.get('image')
        if image:
            from .uploads import is_image
            data = image.read()
            if not is_image(data):
                return Response({'image': ['That file is not an image.']}, status=status.HTTP_400_BAD_REQUEST)
            if entry.image:
                entry.image.delete(save=False)
            entry.image = ContentFile(data, name=image.name)
        entry.status = BacklogEntry.PENDING
        entry.claimed_by = None
        entry.claimed_at = None
        entry.cant_read_note = ''
        entry.save()
        return Response(BacklogEntrySerializer(entry, context={'request': request}).data)

    @action(['GET'], detail=False, url_path='metrics', url_name='metrics')
    def metrics(self, request):
        business = get_active_business(request)
        if not business:
            return Response(NO_BUSINESS, status=status.HTTP_400_BAD_REQUEST)
        since = timezone.localdate().replace(day=1)
        return Response({
            'business': business_metrics(business),
            'people': person_metrics(business, since=since),
        })


class GlossaryViewSet(GenericViewSet):
    """/backlog-glossary/ - the owner keeps the shop's shorthand."""

    permission_classes = [IsAuthenticated, IsBusinessOwner]

    def get_queryset(self):
        business = get_active_business(self.request)
        return (GlossaryEntry.objects.filter(business_id=business.id).current()
                .select_related('customer', 'supplier', 'product').order_by('term'))

    def list(self, request):
        business = get_active_business(request)
        return Response({
            'version': current_version(business),
            'entries': GlossaryEntrySerializer(self.get_queryset(), many=True).data,
        })

    def create(self, request):
        business = get_active_business(request)
        serializer = GlossaryEntryWriteSerializer(data=request.data, context={'business_id': business.id})
        serializer.is_valid(raise_exception=True)
        row = add_term(business, serializer.values(), request.user)
        return Response(GlossaryEntrySerializer(row).data, status=status.HTTP_201_CREATED)

    def update(self, request, pk=None):
        business = get_active_business(request)
        row = self.get_queryset().filter(pk=pk).first()
        if not row:
            return Response({'detail': 'Not Found'}, status=status.HTTP_404_NOT_FOUND)
        serializer = GlossaryEntryWriteSerializer(data=request.data, context={'business_id': business.id},
                                                  partial=True)
        serializer.is_valid(raise_exception=True)
        new_row = change_term(row, serializer.values(), request.user)
        return Response(GlossaryEntrySerializer(new_row).data)

    def partial_update(self, request, pk=None):
        return self.update(request, pk)

    def destroy(self, request, pk=None):
        row = self.get_queryset().filter(pk=pk).first()
        if not row:
            return Response({'detail': 'Not Found'}, status=status.HTTP_404_NOT_FOUND)
        remove_term(row, request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


class GlossarySuggestionViewSet(mixins.ListModelMixin, GenericViewSet):
    """/backlog-glossary-suggestions/ - what entry people proposed."""

    permission_classes = [IsAuthenticated, IsBusinessOwner]
    serializer_class = GlossarySuggestionSerializer

    def get_queryset(self):
        business = get_active_business(self.request)
        return (GlossarySuggestion.objects
                .filter(business_id=business.id, status=GlossarySuggestion.PENDING)
                .select_related('suggested_by', 'customer', 'supplier', 'product')
                .order_by('created_at'))

    @action(['POST'], detail=True, url_path='approve', url_name='approve')
    def approve(self, request, pk=None):
        business = get_active_business(request)
        suggestion = self.get_queryset().filter(pk=pk).first()
        if not suggestion:
            return Response({'detail': 'Not Found'}, status=status.HTTP_404_NOT_FOUND)
        overrides = {}
        if request.data:
            serializer = GlossaryEntryWriteSerializer(data=request.data, partial=True,
                                                      context={'business_id': business.id})
            serializer.is_valid(raise_exception=True)
            overrides = serializer.values()
        row = approve_suggestion(suggestion, request.user, overrides)
        return Response(GlossaryEntrySerializer(row).data)

    @action(['POST'], detail=True, url_path='reject', url_name='reject')
    def reject(self, request, pk=None):
        suggestion = self.get_queryset().filter(pk=pk).first()
        if not suggestion:
            return Response({'detail': 'Not Found'}, status=status.HTTP_404_NOT_FOUND)
        reject_suggestion(suggestion, request.user)
        return Response({'detail': 'OK'})
