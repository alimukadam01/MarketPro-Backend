from rest_framework import serializers

from root.models import Customer, Product, Supplier
from .models import (
    BacklogEntry, BacklogType, GlossaryEntry, GlossaryKind, GlossarySuggestion,
    Priority, UploadBatch,
)
from .outputs import describe, describe_new_record


def person(user):
    if not user:
        return None
    return {'id': user.id, 'name': f'{user.first_name} {user.last_name}'.strip() or user.email}


def image_url(serializer, image):
    """Absolute: the page is served from another origin. A presigned S3 URL
    already is; a local /media/ path gets the host (see PaymentReceiptSerializer)."""
    if not image:
        return None
    request = serializer.context.get('request')
    return request.build_absolute_uri(image.url) if request else image.url


# ── The owner's side ─────────────────────────────────────────────────────────

class UploadBatchSerializer(serializers.ModelSerializer):
    uploaded_by = serializers.SerializerMethodField()
    pages = serializers.IntegerField(source='entries.count', read_only=True)

    def get_uploaded_by(self, batch):
        return person(batch.uploaded_by)

    class Meta:
        model = UploadBatch
        fields = ['id', 'type', 'book_label', 'priority', 'is_daily', 'notes',
                  'skipped_files', 'pages', 'uploaded_by', 'created_at']


class UploadBatchCreateSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=BacklogType.CHOICES)
    book_label = serializers.CharField(max_length=256, required=False, allow_blank=True, default='')
    priority = serializers.ChoiceField(choices=Priority.CHOICES, required=False, default=Priority.NORMAL)
    is_daily = serializers.BooleanField(required=False, default=False)
    notes = serializers.CharField(required=False, allow_blank=True, default='')


class BacklogEntrySerializer(serializers.ModelSerializer):
    """An entry as the owner sees it: everything, including who did what."""

    type_label = serializers.CharField(source='get_type_display', read_only=True)
    status_label = serializers.CharField(source='get_status_display', read_only=True)
    image = serializers.SerializerMethodField()
    claimed_by = serializers.SerializerMethodField()
    entered_by = serializers.SerializerMethodField()
    verified_by = serializers.SerializerMethodField()
    outputs = serializers.SerializerMethodField()
    new_records = serializers.SerializerMethodField()

    def get_image(self, entry):
        return image_url(self, entry.image)

    def get_new_records(self, entry):
        return [describe_new_record(row) for row in entry.new_records.all()]

    def get_claimed_by(self, entry):
        return person(entry.claimed_by)

    def get_entered_by(self, entry):
        return person(entry.entered_by)

    def get_verified_by(self, entry):
        return person(entry.verified_by)

    def get_outputs(self, entry):
        return [describe(output) for output in entry.outputs.all()]

    class Meta:
        model = BacklogEntry
        fields = [
            'id', 'type', 'type_label', 'status', 'status_label', 'batch', 'book_label',
            'priority', 'is_daily', 'image', 'notes', 'claimed_by', 'claimed_at',
            'entered_by', 'entered_at', 'verified_by', 'verified_at', 'cant_read_note',
            'review_note', 'rejection_count', 'seconds_spent', 'glossary_version',
            'completed_list', 'outputs', 'new_records', 'created_at', 'updated_at',
        ]


class BacklogEntryUpdateSerializer(serializers.ModelSerializer):

    class Meta:
        model = BacklogEntry
        fields = ['type', 'book_label', 'priority', 'is_daily', 'notes']


# ── The entry person's side ──────────────────────────────────────────────────

class WorkQueueItemSerializer(serializers.ModelSerializer):
    """A row in the entry queue: what the page is, never whose business data
    it touches."""

    type_label = serializers.CharField(source='get_type_display', read_only=True)
    mine = serializers.SerializerMethodField()

    def get_mine(self, entry):
        request = self.context.get('request')
        return bool(request and entry.claimed_by_id == request.user.id)

    class Meta:
        model = BacklogEntry
        fields = ['id', 'type', 'type_label', 'status', 'book_label', 'priority', 'is_daily',
                  'notes', 'review_note', 'rejection_count', 'mine', 'created_at']


class WorkEntrySerializer(WorkQueueItemSerializer):
    image = serializers.SerializerMethodField()
    outputs = serializers.SerializerMethodField()

    def get_image(self, entry):
        return image_url(self, entry.image)

    def get_outputs(self, entry):
        return [describe(output) for output in entry.outputs.all()]

    class Meta(WorkQueueItemSerializer.Meta):
        fields = WorkQueueItemSerializer.Meta.fields + [
            'image', 'outputs', 'claimed_at', 'completed_list', 'cant_read_note']


# ── Glossary ─────────────────────────────────────────────────────────────────

class _GlossaryLinks(serializers.Serializer):
    """The optional customer / supplier / product a term stands for, kept to
    the business's own records."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        business_id = self.context.get('business_id')
        for name, model in (('customer', Customer), ('supplier', Supplier), ('product', Product)):
            if name in self.fields:
                self.fields[name].queryset = model.objects.filter(business_id=business_id)


class GlossaryEntrySerializer(serializers.ModelSerializer):
    links_to = serializers.SerializerMethodField()

    def get_links_to(self, row):
        for name in ('customer', 'supplier', 'product'):
            record = getattr(row, name)
            if record:
                return {'kind': name, 'id': record.id, 'name': record.name}
        return None

    class Meta:
        model = GlossaryEntry
        fields = ['id', 'term', 'meaning', 'kind', 'customer', 'supplier', 'product',
                  'links_to', 'valid_from', 'source_entry', 'created_at']


class GlossaryEntryWriteSerializer(_GlossaryLinks, serializers.Serializer):
    term = serializers.CharField(max_length=128)
    meaning = serializers.CharField(max_length=256)
    kind = serializers.ChoiceField(choices=GlossaryKind.CHOICES, required=False, default='shorthand')
    customer = serializers.PrimaryKeyRelatedField(queryset=Customer.objects.none(), required=False, allow_null=True)
    supplier = serializers.PrimaryKeyRelatedField(queryset=Supplier.objects.none(), required=False, allow_null=True)
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.none(), required=False, allow_null=True)

    def values(self):
        data = dict(self.validated_data)
        for name in ('customer', 'supplier', 'product'):
            if name in data:
                record = data.pop(name)
                data[f'{name}_id'] = record.id if record else None
        return data


class GlossarySuggestionSerializer(serializers.ModelSerializer):
    suggested_by = serializers.SerializerMethodField()
    links_to = serializers.SerializerMethodField()

    def get_suggested_by(self, suggestion):
        return person(suggestion.suggested_by)

    def get_links_to(self, suggestion):
        return GlossaryEntrySerializer.get_links_to(self, suggestion)

    class Meta:
        model = GlossarySuggestion
        fields = ['id', 'term', 'meaning', 'kind', 'customer', 'supplier', 'product', 'links_to',
                  'note', 'entry', 'suggested_by', 'status', 'created_at']


class GlossarySuggestionCreateSerializer(GlossaryEntryWriteSerializer):
    note = serializers.CharField(required=False, allow_blank=True, default='')
