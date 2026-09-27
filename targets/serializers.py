from calendar import monthrange

from django.db import transaction
from rest_framework import serializers

from core.serializers import SimpleUserSerializer
from root.models import Business

from .catalogue import (
    CHOICE, ENTITY, allowed_dimensions, allowed_subject_types, get_dimension,
    get_spec, resolve_choice_label, resolve_entity_label,
)
from .models import ManualDataPoint, Target, TargetFilter
from .utils import period_label, resolve_period


### Filters ---------------------------------------------------------------

class TargetFilterSerializer(serializers.ModelSerializer):

    class Meta:
        model = TargetFilter
        fields = ['id', 'dimension', 'value_id', 'value_text', 'label']


class TargetFilterWriteSerializer(serializers.Serializer):
    """
    A filter as it arrives from the client. Validated by the parent, which is
    the only place that knows the data point and so which dimensions are legal.
    """
    dimension = serializers.CharField()
    value_id = serializers.IntegerField(required=False, allow_null=True)
    value_text = serializers.CharField(required=False, allow_null=True,
                                       allow_blank=True)


### Shared validation -----------------------------------------------------

class TargetValidationMixin:
    """
    Cross-field validation shared by create and update.

    The tenant guarantee here is written out by hand rather than expressed with
    PrimaryKeyRelatedField(queryset=...none()) and re-narrowed in __init__, the
    way backlog does it. That trick needs a real ForeignKey, and a target's
    subject and filter values are plain integers on purpose. It was not
    forgotten; see the note on TargetFilter.
    """

    # Only threshold is implemented. Accepting another rule would mean storing a
    # target whose status is computed by the wrong logic, which is worse than
    # refusing it.
    SUPPORTED_RULES = ('threshold',)

    def _business(self):
        business_id = self.context.get('business_id')
        return Business.objects.filter(id=business_id).first()

    def _validate_period(self, attrs, instance=None):
        """
        Every period type needs its own fields, and only its own. Fields
        belonging to the other types are cleared, so switching a target from a
        custom range to a month cannot leave a stale date_to behind to confuse
        a later reader.
        """
        def value(name):
            if name in attrs:
                return attrs[name]
            return getattr(instance, name, None) if instance else None

        period_type = value('period_type')
        errors = {}

        if period_type == 'month':
            year, month = value('period_year'), value('period_month')
            if not year:
                errors['period_year'] = 'A year is required for a monthly target.'
            if not month or not 1 <= month <= 12:
                errors['period_month'] = 'A month between 1 and 12 is required.'

        elif period_type == 'quarter':
            year, quarter = value('period_year'), value('period_quarter')
            if not year:
                errors['period_year'] = 'A year is required for a quarterly target.'
            if not quarter or not 1 <= quarter <= 4:
                errors['period_quarter'] = 'A quarter between 1 and 4 is required.'

        elif period_type == 'year':
            if not value('period_year'):
                errors['period_year'] = 'A year is required.'

        elif period_type == 'custom':
            date_from, date_to = value('date_from'), value('date_to')
            if not date_from:
                errors['date_from'] = 'A start date is required for a custom range.'
            if not date_to:
                errors['date_to'] = 'An end date is required for a custom range.'
            if date_from and date_to and date_to < date_from:
                errors['date_to'] = 'The end date cannot fall before the start date.'

        elif period_type == 'rolling':
            days = value('rolling_days')
            if not days or days < 1:
                errors['rolling_days'] = 'A rolling window needs at least one day.'

        if errors:
            raise serializers.ValidationError(errors)

        keep = {
            'month': ('period_year', 'period_month'),
            'quarter': ('period_year', 'period_quarter'),
            'year': ('period_year',),
            'custom': ('date_from', 'date_to'),
            'rolling': ('rolling_days',),
        }[period_type]

        for name in ('period_year', 'period_month', 'period_quarter',
                     'date_from', 'date_to', 'rolling_days'):
            if name not in keep:
                attrs[name] = None

    def _validate_scope(self, attrs, data_point, instance=None):
        def value(name):
            if name in attrs:
                return attrs[name]
            return getattr(instance, name, None) if instance else None

        scope_type = value('scope_type') or 'business'

        if scope_type != 'subject':
            attrs['subject_type'] = None
            attrs['subject_id'] = None
            attrs['subject_label'] = None
            return

        subject_type = value('subject_type')
        subject_id = value('subject_id')

        allowed = allowed_subject_types(data_point)
        if subject_type not in allowed:
            raise serializers.ValidationError({
                'subject_type': (
                    f'{get_spec(data_point).label} cannot be measured per '
                    f'{subject_type or "subject"}. '
                    f'Available: {", ".join(allowed) or "none"}.'
                )
            })

        if not subject_id:
            raise serializers.ValidationError(
                {'subject_id': 'A subject is required when the scope is one subject.'})

        business = self._business()
        dimension_name = 'entered_by' if subject_type == 'employee' else subject_type
        label = resolve_entity_label(dimension_name, subject_id, business)
        if not label:
            raise serializers.ValidationError(
                {'subject_id': 'That subject does not belong to this business.'})

        # Never taken from the client.
        attrs['subject_label'] = label

    def _validate_filters(self, filters, data_point):
        allowed = allowed_dimensions(data_point)
        business = self._business()
        seen = set()
        resolved = []

        for row in filters:
            dimension_name = row.get('dimension')

            if dimension_name not in allowed:
                raise serializers.ValidationError({
                    'filters': (
                        f'{get_spec(data_point).label} cannot be filtered by '
                        f'{dimension_name}. '
                        f'Available: {", ".join(allowed) or "none"}.'
                    )
                })

            if dimension_name in seen:
                raise serializers.ValidationError(
                    {'filters': f'{dimension_name} is filtered more than once.'})
            seen.add(dimension_name)

            dimension = get_dimension(dimension_name)

            if dimension['kind'] == ENTITY:
                value_id = row.get('value_id')
                if not value_id:
                    raise serializers.ValidationError(
                        {'filters': f'{dimension_name} needs a value_id.'})
                label = resolve_entity_label(dimension_name, value_id, business)
                if not label:
                    raise serializers.ValidationError({
                        'filters': (
                            f'That {dimension_name} does not belong to this '
                            f'business.'
                        )
                    })
                resolved.append({'dimension': dimension_name,
                                 'value_id': value_id, 'value_text': None,
                                 'label': label})
            else:
                value_text = row.get('value_text')
                label = resolve_choice_label(dimension_name, value_text, data_point)
                if not label:
                    raise serializers.ValidationError({
                        'filters': f'{value_text} is not a valid {dimension_name}.'
                    })
                resolved.append({'dimension': dimension_name, 'value_id': None,
                                 'value_text': value_text, 'label': label})

        return resolved

    def validate(self, attrs):
        instance = getattr(self, 'instance', None)

        data_point = attrs.get(
            'data_point', getattr(instance, 'data_point', None))
        if not get_spec(data_point):
            raise serializers.ValidationError(
                {'data_point': f'{data_point} is not a data point.'})

        kind = attrs.get('data_point_kind',
                         getattr(instance, 'data_point_kind', 'base'))
        if kind != 'base':
            raise serializers.ValidationError({
                'data_point_kind': 'Composite data points are not available yet.'
            })

        rule_type = attrs.get('rule_type',
                              getattr(instance, 'rule_type', 'threshold'))
        if rule_type not in self.SUPPORTED_RULES:
            raise serializers.ValidationError({
                'rule_type': (
                    f'{rule_type} is not implemented yet. Only threshold '
                    f'targets can be measured today.'
                )
            })

        target_value = attrs.get('target_value',
                                 getattr(instance, 'target_value', 0))
        if not target_value or target_value <= 0:
            raise serializers.ValidationError(
                {'target_value': 'A target needs a number greater than zero.'})

        self._validate_period(attrs, instance)
        self._validate_scope(attrs, data_point, instance)

        if 'filters' in self.initial_data:
            raw = self.initial_data.get('filters') or []
            attrs['_resolved_filters'] = self._validate_filters(raw, data_point)

        return attrs


### Targets ---------------------------------------------------------------

class SimpleTargetSerializer(serializers.ModelSerializer):

    data_point_label = serializers.SerializerMethodField()

    class Meta:
        model = Target
        fields = [
            'id', 'name', 'data_point', 'data_point_label', 'period_type',
            'scope_type', 'subject_label', 'target_value', 'rule_type',
            'created_at',
        ]

    def get_data_point_label(self, target):
        spec = get_spec(target.data_point)
        return spec.label if spec else target.data_point


class TargetSerializer(serializers.ModelSerializer):
    """
    The full target. Deliberately carries no figure: what has been achieved is
    computed on read and belongs to the KPI endpoints, so a retrieve stays a
    cheap read of the configuration.
    """

    filters = TargetFilterSerializer(many=True, read_only=True)
    created_by = SimpleUserSerializer(read_only=True)
    data_point_label = serializers.SerializerMethodField()
    unit = serializers.SerializerMethodField()
    date_basis_label = serializers.SerializerMethodField()
    returns_treatment = serializers.SerializerMethodField()
    period_label = serializers.SerializerMethodField()
    date_from_resolved = serializers.SerializerMethodField()
    date_to_resolved = serializers.SerializerMethodField()
    is_open = serializers.SerializerMethodField()

    class Meta:
        model = Target
        fields = [
            'id', 'business', 'name', 'data_point_kind', 'data_point',
            'data_point_label', 'unit', 'date_basis_label', 'returns_treatment',
            'scope_type', 'subject_type', 'subject_id', 'subject_label',
            'period_type', 'period_year', 'period_month', 'period_quarter',
            'date_from', 'date_to', 'rolling_days',
            'period_label', 'date_from_resolved', 'date_to_resolved', 'is_open',
            'target_value', 'rule_type', 'outcome', 'notes',
            'filters', 'created_by', 'created_at', 'updated_at',
        ]

    def _spec(self, target):
        return get_spec(target.data_point)

    def get_data_point_label(self, target):
        spec = self._spec(target)
        return spec.label if spec else target.data_point

    def get_unit(self, target):
        spec = self._spec(target)
        return spec.unit if spec else 'count'

    def get_date_basis_label(self, target):
        spec = self._spec(target)
        return spec.date_basis_label if spec else ''

    def get_returns_treatment(self, target):
        spec = self._spec(target)
        return spec.returns_treatment if spec else ''

    def _window(self, target):
        try:
            return resolve_period(target)
        except Exception as error:
            print(error)
            return None, None

    def get_period_label(self, target):
        date_from, date_to = self._window(target)
        if not date_from:
            return ''
        return period_label(target, date_from, date_to)

    def get_date_from_resolved(self, target):
        date_from, _ = self._window(target)
        return date_from.isoformat() if date_from else None

    def get_date_to_resolved(self, target):
        _, date_to = self._window(target)
        return date_to.isoformat() if date_to else None

    def get_is_open(self, target):
        from root.utils import local_date
        _, date_to = self._window(target)
        if not date_to:
            return False
        return target.period_type == 'rolling' or local_date() <= date_to


WRITABLE_FIELDS = [
    'name', 'data_point_kind', 'data_point',
    'scope_type', 'subject_type', 'subject_id',
    'period_type', 'period_year', 'period_month', 'period_quarter',
    'date_from', 'date_to', 'rolling_days',
    'target_value', 'rule_type', 'outcome', 'notes',
]


class TargetCreateSerializer(TargetValidationMixin, serializers.ModelSerializer):

    filters = TargetFilterWriteSerializer(many=True, required=False)

    class Meta:
        model = Target
        # id is read-only but has to be in the response: a client that has just
        # created a target needs to know which one it made.
        fields = ['id'] + WRITABLE_FIELDS + ['filters']
        read_only_fields = ['id']

    def save(self, **kwargs):
        data = dict(self.validated_data)
        data.pop('filters', None)
        resolved = data.pop('_resolved_filters', [])

        with transaction.atomic():
            target = Target.objects.create(
                business_id=self.context['business_id'],
                created_by_id=self.context['user_id'],
                **data
            )
            if resolved:
                TargetFilter.objects.bulk_create([
                    TargetFilter(target=target, **row) for row in resolved
                ])

        # DRF renders serializer.data from validated_data when instance is
        # unset, which drops the new id and every field that fell back to a
        # model default. A client needs the id of what it just created.
        self.instance = target
        return target


class TargetUpdateSerializer(TargetValidationMixin, serializers.ModelSerializer):

    filters = TargetFilterWriteSerializer(many=True, required=False)

    class Meta:
        model = Target
        fields = WRITABLE_FIELDS + ['filters']

    def validate(self, attrs):
        from root.utils import local_date

        # A closed period is a closed record. Every other attribute is editable
        # while the period runs.
        _, date_to = resolve_period(self.instance)
        if self.instance.period_type != 'rolling' and local_date() > date_to:
            raise serializers.ValidationError({
                'detail': (
                    "This target's period has ended. It can be duplicated or "
                    "deleted, but not edited."
                )
            })

        return super().validate(attrs)

    def update(self, instance, validated_data):
        data = dict(validated_data)
        data.pop('filters', None)
        resolved = data.pop('_resolved_filters', None)

        with transaction.atomic():
            for attr, value in data.items():
                setattr(instance, attr, value)
            instance.save()

            # Replaced wholesale rather than reconciled row by row: the client
            # sends the set it wants, and a filter carries no history worth
            # preserving.
            if resolved is not None:
                instance.filters.all().delete()
                if resolved:
                    TargetFilter.objects.bulk_create([
                        TargetFilter(target=instance, **row) for row in resolved
                    ])

        return instance


### Manual data points ----------------------------------------------------

class SimpleManualDataPointSerializer(serializers.ModelSerializer):

    class Meta:
        model = ManualDataPoint
        # notes is included because the card shows it when expanded, and the
        # dashboard never fetches a data point on its own.
        fields = ['id', 'name', 'value', 'value_type', 'as_of_date', 'notes',
                  'created_at']


class ManualDataPointSerializer(serializers.ModelSerializer):

    created_by = SimpleUserSerializer(read_only=True)

    class Meta:
        model = ManualDataPoint
        fields = ['id', 'business', 'name', 'value', 'value_type',
                  'as_of_date', 'notes', 'created_by', 'created_at',
                  'updated_at']


class ManualDataPointCreateSerializer(serializers.ModelSerializer):

    class Meta:
        model = ManualDataPoint
        fields = ['id', 'name', 'value', 'value_type', 'as_of_date', 'notes']
        read_only_fields = ['id']

    def save(self, **kwargs):
        # Assigned, not just returned: see the note in TargetCreateSerializer.
        self.instance = ManualDataPoint.objects.create(
            business_id=self.context['business_id'],
            created_by_id=self.context['user_id'],
            **self.validated_data
        )
        return self.instance


class ManualDataPointUpdateSerializer(serializers.ModelSerializer):

    class Meta:
        model = ManualDataPoint
        fields = ['name', 'value', 'value_type', 'as_of_date', 'notes']

    def update(self, instance, validated_data):
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        return instance
