from rest_framework import serializers
from django.db import transaction
from django.db.models import Q
from django.contrib.auth import get_user_model
from .models import (
    ProductField,
    ProductFieldLink,
    City,
    Category,
    Expense,
    Product,
    ProductVariantType,
    ProductVariant,
    Supplier,
    Unit,
    Business,
    Customer,
    Location,
    Employee,
    EmployeeAccess,
    BusinessConfig,
    Person,
)
from .new_records import OpeningBalanceSerializer
from .utils import generate_sku, generate_variant_name
from audit.helpers import audited_bulk_create, audited_bulk_update

User = get_user_model()


class CitySerializer(serializers.ModelSerializer):

    class Meta:
        model = City
        fields = ['id', 'name', 'postal_code']


class CategorySerializer(serializers.ModelSerializer):

    class Meta:
        model = Category
        fields = ['id', 'name', 'desc']


class UnitSerializer(serializers.ModelSerializer):

    class Meta:
        model = Unit
        fields = ['id', 'name', 'abv']


# ── BusinessConfig ────────────────────────────────────────────────────────────

class BusinessConfigSerializer(serializers.ModelSerializer):

    class Meta:
        model = BusinessConfig
        fields = [
            'id', 'business',
            'sales', 'purchases', 'projects',
            'inventory', 'returned_items', 'quotations', 'accounting',
            'targets',
        ]


class SimpleBusinessConfigSerializer(serializers.ModelSerializer):

    class Meta:
        model = BusinessConfig
        fields = [
            'sales', 'purchases', 'projects',
            'inventory', 'returned_items', 'quotations', 'accounting',
            'targets',
        ]


class BusinessConfigCreateSerializer(serializers.ModelSerializer):

    class Meta:
        model = BusinessConfig
        fields = [
            'sales', 'purchases', 'projects',
            'inventory', 'returned_items', 'quotations', 'accounting',
            'targets',
        ]

    def create(self, validated_data):
        return BusinessConfig.objects.create(
            business_id=self.context['business_id'],
            **validated_data,
        )


class BusinessConfigUpdateSerializer(serializers.ModelSerializer):

    class Meta:
        model = BusinessConfig
        fields = [
            'sales', 'purchases', 'projects',
            'inventory', 'returned_items', 'quotations', 'accounting',
            'targets',
        ]

    def update(self, instance, validated_data):
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        return instance


class BusinessSerializer(serializers.ModelSerializer):
    config = SimpleBusinessConfigSerializer()
    class Meta:
        model = Business
        fields = [
            'id', 'name', 'owner', 'phone',
            'address', 'logo', 'is_active', 'config'
        ]


class BusinessCreateSerializer(serializers.ModelSerializer):

    class Meta:
        model = Business
        fields = ['name', 'phone', 'logo']

    def save(self, **kwargs):
        return Business.objects.create(owner_id=self.context['owner_id'], **self.validated_data)


class SimpleBusinessSerializer(serializers.ModelSerializer):

    class Meta:
        model = Business
        fields = ['id', 'name', 'logo']


def _detail_text(field, **kwargs):
    """An optional text detail read from and written to the role's person."""
    return field(source=f'person.{kwargs.pop("name")}', required=False,
                 allow_null=True, allow_blank=True, **kwargs)


class RoleDetailsMixin:
    """
    For a Customer or Supplier serializer whose detail fields come from the
    role's Person (fields with source='person.<detail>'). The API is the same
    as when the details lived on the role: same names, same rules. Creating
    makes the person (RoleManager.create); updating writes to it.
    """

    def create(self, validated_data):
        details = validated_data.pop('person', {})
        return self.Meta.model.objects.create(
            business_id=self.context['business_id'],
            **details, **validated_data
        )

    def update(self, instance, validated_data):
        details = validated_data.pop('person', {})
        if details:
            for attr, value in details.items():
                setattr(instance.person, attr, value)
            instance.person.save()

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        instance.save()
        return instance


class CustomerSerializer(RoleDetailsMixin, serializers.ModelSerializer):

    business = SimpleBusinessSerializer(read_only=True)
    name = serializers.CharField(source='person.name', max_length=256)
    phone = _detail_text(serializers.CharField, name='phone', max_length=256)
    email = serializers.EmailField(source='person.email', required=False,
                                   allow_null=True, allow_blank=True)
    address = _detail_text(serializers.CharField, name='address')
    # Required for customers, as it always was (register S5).
    city = serializers.PrimaryKeyRelatedField(
        source='person.city', queryset=City.objects.all())
    notes = _detail_text(serializers.CharField, name='notes')

    class Meta:
        model = Customer
        fields = [
            'id', 'name', 'business', 'phone', 'email',
            'address', 'city', 'created_at', 'updated_at',
            'notes'
        ]


class CaptureCustomerSerializer(CustomerSerializer):
    """
    The walk-in capture form on invoice download.

    Everything is inherited from CustomerSerializer except the phone, which is
    optional on the customer screen but not here: the invoice goes out over
    WhatsApp to this number the moment it saves.
    """

    phone = serializers.CharField(source='person.phone', max_length=256,
                                  required=True, allow_blank=False)


class SimpleCustomerSerializer(serializers.ModelSerializer):

    name = serializers.CharField(source='person.name', read_only=True)
    phone = serializers.CharField(source='person.phone', read_only=True)
    email = serializers.EmailField(source='person.email', read_only=True)
    address = serializers.CharField(source='person.address', read_only=True)
    city = CitySerializer(source='person.city', read_only=True)

    class Meta:
        model = Customer
        fields = [
            'id', 'name', 'phone',
            'email', 'address', 'city'
        ]


class LocationSerializer(serializers.ModelSerializer):

    business = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = Location
        fields = ['id', 'business', 'name', 'address', 'created_at']

    def create(self, validated_data):
        return Location.objects.create(
            business_id=self.context['business_id'],
            **validated_data
        )

    def update(self, instance, validated_data):
        return super().update(instance, validated_data)


class SimpleLocationSerializer(serializers.Serializer):

    class Meta:
        model = Location
        fields = ['id', 'name', 'address']


class ProductVariantTypeSerializer(serializers.ModelSerializer):

    class Meta:
        model = ProductVariantType
        fields = ['id', 'name']


class SimpleProductVariantSerializer(serializers.ModelSerializer):

    class Meta:
        model = ProductVariant
        fields = ['id', 'name', 'sku', 'attributes', 'is_active']


class ProductSerializer(serializers.ModelSerializer):

    business = SimpleBusinessSerializer(read_only=True)
    code = serializers.CharField()
    unit = UnitSerializer(read_only=True)
    variants = SimpleProductVariantSerializer(many=True)
    # What the update form's "Asked on invoices" section is filled from.
    field_links = serializers.SerializerMethodField()

    def get_field_links(self, obj):
        return [
            {'field': link.field_id, 'label': link.field.label,
             'field_type': link.field.field_type, 'is_active': link.field.is_active,
             'applies_to': link.applies_to, 'required': link.required}
            for link in obj.field_links.select_related('field').order_by('field__label')
        ]

    class Meta:
        model = Product
        fields = [
            'id', 'business', 'code', 'name', 'desc',
            'unit', 'created_at', 'updated_at', 'variants', 'field_links'
        ]


def asked_fields(product):
    """The extra fields a product asks on invoice lines, as the screens need
    them. Through .all(), so a prefetch of field_links__field is used; retired
    fields are left out - a new line is never asked for one."""
    return [
        {'id': link.field_id, 'label': link.field.label, 'key': link.field.key,
         'field_type': link.field.field_type, 'applies_to': link.applies_to,
         'required': link.required}
        for link in sorted(product.field_links.all(), key=lambda link: link.field.label.lower())
        if link.field.is_active
    ]


class SimpleProductSerializer(serializers.ModelSerializer):

    unit = UnitSerializer()
    num_variants = serializers.SerializerMethodField()
    # The extra fields its invoice lines ask for (plans/product-extra-fields.md).
    extra_fields = serializers.SerializerMethodField()

    def get_num_variants(self, obj):
        return len(obj.variants.all())

    def get_extra_fields(self, obj):
        return asked_fields(obj)

    class Meta:
        model = Product
        fields = [
            'id', 'business', 'code', 'name', 'desc',
            'unit', 'created_at', 'updated_at', 'num_variants', 'extra_fields'
        ]


class ProductFieldSerializer(serializers.ModelSerializer):
    """A business's extra field, for the product form's checklist."""

    class Meta:
        model = ProductField
        fields = ['id', 'label', 'key', 'field_type']


class FieldLinksMixin:
    """field_links on the product create and update serializers: which of the
    business's extra fields this product asks for, [{field, applies_to,
    required}]. Sent as the whole set; leaving it out leaves the links alone,
    so a client that does not know about them cannot wipe them."""

    def validate_field_links(self, links):
        if links is None:
            return []
        if not isinstance(links, list):
            raise serializers.ValidationError('field_links must be a list.')
        business_id = self.context.get('business_id')
        # Active fields, and any retired one this product already asks for:
        # saving the form must not fail over a field retired since.
        allowed = Q(is_active=True)
        if getattr(self, 'instance', None) is not None:
            allowed |= Q(links__product=self.instance)
        fields = {
            field.id: field for field in
            ProductField.objects.filter(allowed, business_id=business_id).distinct()
        }
        cleaned, seen = [], set()
        for link in links:
            if not isinstance(link, dict):
                raise serializers.ValidationError('Each field link is {field, applies_to, required}.')
            try:
                field_id = int(link.get('field'))
            except (TypeError, ValueError):
                raise serializers.ValidationError('Pick an extra field.')
            if field_id not in fields:
                raise serializers.ValidationError('That extra field is not one of this business\'s.')
            if field_id in seen:
                raise serializers.ValidationError(f'{fields[field_id].label} is listed twice.')
            seen.add(field_id)
            applies_to = link.get('applies_to') or ProductFieldLink.SALE
            if applies_to not in dict(ProductFieldLink.APPLIES_TO_CHOICES):
                raise serializers.ValidationError('Ask on sales, purchases or both.')
            cleaned.append({'field_id': field_id, 'applies_to': applies_to,
                            'required': bool(link.get('required'))})
        return cleaned

    def save_field_links(self, product, links):
        """Make the product's links exactly `links`."""
        keep = {link['field_id'] for link in links}
        ProductFieldLink.objects.filter(product=product).exclude(field_id__in=keep).delete()
        current = {link.field_id: link for link in ProductFieldLink.objects.filter(product=product)}
        for link in links:
            existing = current.get(link['field_id'])
            if existing is None:
                ProductFieldLink.objects.create(product=product, **link)
            elif (existing.applies_to, existing.required) != (link['applies_to'], link['required']):
                existing.applies_to, existing.required = link['applies_to'], link['required']
                existing.save(update_fields=['applies_to', 'required'])


class ProductCreateUpdateSerializer(serializers.ModelSerializer):

    class Meta:
        model = Product
        fields = [
            'id', 'name', 'desc', 'code',
            'unit', 'created_at', 'updated_at',
        ]

    def create(self, validated_data):
        return Product.objects.create(
            business_id=self.context['business_id'],
            **validated_data
        )

    def update(self, instance, validated_data):
        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        instance.save()
        return instance


class ProductVariantSerializer(serializers.ModelSerializer):

    base = SimpleProductSerializer(read_only=True)

    class Meta:
        model = ProductVariant
        fields = ['id', 'base', 'name', 'sku', 'attributes', 'is_active']


class ProductVariantCreateSerializer(serializers.ModelSerializer):

    name = serializers.CharField(required=False)

    def save(self, **kwargs):

        if not self.validated_data.get("name"):
            self.validated_data['name'] = generate_variant_name(
                self.validated_data.get('attributes')
            )

        sku = generate_sku(self.context['business_id'])
        return ProductVariant.objects.create(
            base_id=self.context['product_id'],
            sku=sku,
            **self.validated_data
        )

    class Meta:
        model = ProductVariant
        fields = ['id', 'name', 'attributes']


class ProductAndVariantUpdateSerializer(FieldLinksMixin, serializers.ModelSerializer):

    variants = serializers.ListField()
    field_links = serializers.ListField(required=False)

    def save(self, **kwargs):

        variants = self.validated_data.pop('variants', [])
        field_links = self.validated_data.pop('field_links', None)
        instance = self.instance
        # Only when sent: see FieldLinksMixin.
        if field_links is not None:
            with transaction.atomic():
                self.save_field_links(instance, field_links)

        if variants:
            # separate new and old variants
            new_variants = [v for v in variants if not v.get("id")]
            existing_variants = ProductVariant.objects.filter(base=instance)
            existing_variant_ids = set([var.id for var in existing_variants])
            existing_variant_pData = [v for v in variants if v.get("id")]
            existing_variant_pIds = set([v['id'] for v in variants if v.get("id")])

            # Determine variants to be deleted
            tbd_variants = existing_variant_ids - existing_variant_pIds

            # create old variants id map
            existing_variants_map = {var.id: var for var in existing_variants}

            # update product attrs
            for attr, value in self.validated_data.items():
                setattr(instance, attr, value)

            # create new variant objects list
            tbc_variants = [ProductVariant(
                name=generate_variant_name(var['attributes']),
                sku=generate_sku(self.context['business_id']),
                base=instance,
                **var
            ) for var in new_variants]

            tbu_variants = []
            for data in existing_variant_pData:
                variant = existing_variants_map[data['id']]
                attributes = data.pop('attributes', {})
                variant.name = generate_variant_name(attributes)
                variant.attributes = attributes
                tbu_variants.append(variant)

            try:
                with transaction.atomic():
                    ProductVariant.objects.filter(id__in=tbd_variants).delete()
                    audited_bulk_create(ProductVariant, tbc_variants)
                    audited_bulk_update(
                        ProductVariant, tbu_variants, fields=['name', 'attributes']
                    )
                    instance.save()
            except Exception as error:
                # Re-raised, not swallowed: a half-written variant set
                # must not read as a successful update.
                print(f"Error updating objects: {error}")
                raise
        else:
            try:
                with transaction.atomic():
                    ProductVariant.objects.filter(base=instance).delete()
                    ProductVariant.objects.create(
                        base=instance,
                        sku=generate_sku(business_id=self.context['business_id']),
                        name=generate_variant_name(),
                    )
            except Exception as error:
                # Re-raised, not swallowed: a half-written variant set
                # must not read as a successful update.
                print(f"Error updating objects: {error}")
                raise

        return instance

    class Meta:
        model = Product
        fields = ['id', 'name', 'unit', 'desc', 'variants', 'field_links']


class ProductAndVariantCreateSerializer(FieldLinksMixin, serializers.ModelSerializer):

    variants = serializers.ListField()
    field_links = serializers.ListField(required=False)

    def save(self, **kwargs):

        sku_count = 0
        try:
            with transaction.atomic():
                variants = self.validated_data.pop('variants', [])
                field_links = self.validated_data.pop('field_links', None) or []
                product = Product.objects.create(
                    business_id=self.context['business_id'], **self.validated_data
                )
                self.save_field_links(product, field_links)

                if variants:
                    new_variants = []
                    for variant in variants:
                        new_variants.append(ProductVariant(
                            base=product,
                            sku=generate_sku(
                                business_id=self.context['business_id']),
                            name=generate_variant_name(variant['attributes']),
                            **variant
                        ))
                        sku_count += 1

                    audited_bulk_create(ProductVariant, new_variants)
                else:
                    ProductVariant.objects.create(
                        base=product,
                        sku=generate_sku(business_id=self.context['business_id']),
                        name=generate_variant_name(),
                    )

        except Exception as error:
            # Give back the SKU numbers the failed batch took, then fail: this
            # used to fall through and return a `product` never assigned.
            print(f"Error creating product variants: {error}")
            business = Business.objects.get(pk=self.context['business_id'])
            business.sku_counter -= sku_count
            business.save(update_fields=['sku_counter'])
            raise

        return product

    class Meta:
        model = Product
        fields = ['id', 'name', 'desc', 'unit', 'variants', 'field_links']


class SupplierSerializer(RoleDetailsMixin, serializers.ModelSerializer):

    business = SimpleBusinessSerializer(read_only=True)
    name = serializers.CharField(source='person.name', max_length=256)
    business_name = _detail_text(
        serializers.CharField, name='business_name', max_length=256)
    phone = _detail_text(serializers.CharField, name='phone', max_length=256)
    email = serializers.EmailField(source='person.email', required=False,
                                   allow_null=True, allow_blank=True)
    notes = _detail_text(serializers.CharField, name='notes')

    class Meta:
        model = Supplier
        fields = [
            'id', 'business', 'name', 'business_name', 'phone', 'email', 'notes'
        ]


class SimpleSupplierSerializer(serializers.ModelSerializer):

    name = serializers.CharField(source='person.name', read_only=True)
    business_name = serializers.CharField(
        source='person.business_name', read_only=True)

    class Meta:
        model = Supplier
        fields = [
            'id', 'name', 'business_name',
        ]


# ── People ────────────────────────────────────────────────────────────────────

PERSON_DETAIL_FIELDS = [
    'name', 'phone', 'email', 'address', 'city', 'business_name', 'notes',
]


class PersonSerializer(serializers.ModelSerializer):
    """
    One row of the People list. Balances and last activity are worked out for
    the whole page at once and passed in the context (root/people.py).
    """

    city = CitySerializer(read_only=True)
    roles = serializers.SerializerMethodField()
    receivable = serializers.SerializerMethodField()
    payable = serializers.SerializerMethodField()
    net = serializers.SerializerMethodField()
    last_activity = serializers.SerializerMethodField()
    is_walk_in = serializers.SerializerMethodField()

    class Meta:
        model = Person
        fields = [
            'id', *PERSON_DETAIL_FIELDS, 'created_at', 'updated_at',
            'roles', 'receivable', 'payable', 'net', 'last_activity', 'is_walk_in',
        ]

    def get_roles(self, person):
        from .people import ROLES, role_of
        roles = {}
        for kind in ROLES:
            role = role_of(person, kind)
            roles[kind] = {'id': role.id, 'is_active': role.is_active} if role else None
        return roles

    def _sides(self, person):
        return self.context.get('balances', {}).get(person.id) or {'receivable': 0, 'payable': 0}

    def get_receivable(self, person):
        return self._sides(person)['receivable']

    def get_payable(self, person):
        return self._sides(person)['payable']

    def get_net(self, person):
        sides = self._sides(person)
        return sides['receivable'] - sides['payable']

    def get_last_activity(self, person):
        day = self.context.get('last_activity', {}).get(person.id)
        return day.isoformat() if day else None

    def get_is_walk_in(self, person):
        from .people import is_walk_in
        return is_walk_in(person)


class PersonDetailSerializer(PersonSerializer):
    """The person page: the list row plus each side's opening balance."""

    opening_balances = serializers.SerializerMethodField()

    class Meta(PersonSerializer.Meta):
        fields = PersonSerializer.Meta.fields + ['opening_balances']

    def get_opening_balances(self, person):
        from .people import role_of
        sides = {}
        for kind, side in (('customer', 'receivable'), ('supplier', 'payable')):
            role = role_of(person, kind)
            opening = getattr(role, 'opening_balance', None) if role else None
            sides[side] = ({'amount': opening.amount, 'as_of_date': opening.as_of_date.isoformat()}
                           if opening else None)
        return sides


class PersonWriteSerializer(serializers.ModelSerializer):
    """
    Create a person, with roles and an opening balance per side, or edit
    their details. Roles change through /people/<id>/roles/ afterwards.
    """

    city = serializers.PrimaryKeyRelatedField(
        queryset=City.objects.all(), required=False, allow_null=True)
    roles = serializers.ListField(
        child=serializers.ChoiceField(choices=['customer', 'supplier']),
        required=False, write_only=True)
    opening_balances = serializers.DictField(
        child=OpeningBalanceSerializer(allow_null=True), required=False, write_only=True)

    class Meta:
        model = Person
        fields = PERSON_DETAIL_FIELDS + ['roles', 'opening_balances']

    def validate_opening_balances(self, value):
        unknown = set(value) - {'receivable', 'payable'}
        if unknown:
            raise serializers.ValidationError('Use receivable and payable only.')
        return {side: entry for side, entry in value.items() if entry and entry.get('amount')}

    def validate(self, attrs):
        if self.instance is not None and ('roles' in attrs or 'opening_balances' in attrs):
            raise serializers.ValidationError(
                'Roles change through /people/<id>/roles/; opening balances on their own screen.')
        roles = set(attrs.get('roles', []))
        openings = attrs.get('opening_balances', {})
        if 'receivable' in openings and 'customer' not in roles:
            raise serializers.ValidationError(
                {'opening_balances': 'A Receivable opening balance needs the customer role.'})
        if 'payable' in openings and 'supplier' not in roles:
            raise serializers.ValidationError(
                {'opening_balances': 'A Payable opening balance needs the supplier role.'})
        return attrs

    def create(self, validated_data):
        from accounts.models import PartyOpeningBalance
        roles = validated_data.pop('roles', [])
        openings = validated_data.pop('opening_balances', {})
        business_id = self.context['business_id']
        with transaction.atomic():
            person = Person.objects.create(business_id=business_id, **validated_data)
            made = {}
            for kind, model in (('customer', Customer), ('supplier', Supplier)):
                if kind in roles:
                    made[kind] = model.objects.create(business_id=business_id, person=person)
            for side, kind in (('receivable', 'customer'), ('payable', 'supplier')):
                if side in openings:
                    PartyOpeningBalance.objects.create(
                        business_id=business_id, amount=openings[side]['amount'],
                        as_of_date=openings[side]['as_of_date'], **{kind: made[kind]})
        return person


class BaseItemSerializer(serializers.Serializer):

    id = serializers.IntegerField(read_only=True)
    business = SimpleBusinessSerializer(read_only=True)
    product = serializers.PrimaryKeyRelatedField(
        queryset=ProductVariant.objects.none()
    )
    quantity = serializers.IntegerField()
    track_code = serializers.CharField(required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        business_id = self.context.get('business_id')
        if business_id:
            self.fields['product'].queryset = ProductVariant.objects.filter(
                base__business_id=business_id)

            if self.fields.get('location'):
                self.fields['location'].queryset = Location.objects.filter(
                    business_id=business_id)


class ExpenseSerializer(serializers.ModelSerializer):

    business = serializers.PrimaryKeyRelatedField(read_only=True)

    # Money details land on the linked transaction, which the signal creates.
    account = serializers.IntegerField(
        required=False, allow_null=True, write_only=True)
    date = serializers.DateField(
        required=False, allow_null=True, write_only=True)

    # ...and have to come back out again, or the update screen cannot show
    # which account the money was paid from. account/date above are write_only,
    # so the detail response carried neither and the form fell back to blank.
    paid_from = serializers.SerializerMethodField()
    paid_on = serializers.SerializerMethodField()
    # For the list: the account by name, and the expense's one date - the
    # day the money went out, as the transactions list shows it.
    paid_from_name = serializers.SerializerMethodField()
    expense_date = serializers.SerializerMethodField()
    # So a screen can lock an expense the invoice owns, and say which invoice.
    is_invoice_charge = serializers.BooleanField(read_only=True)
    sales_invoice = serializers.PrimaryKeyRelatedField(read_only=True)

    def validate_category(self, value):
        # Made by the system for invoice extra charges, and kept in step with
        # the invoice; one typed in by hand would be neither.
        if value == Expense.INVOICE_CHARGE_CATEGORY:
            raise serializers.ValidationError(
                'Invoice extra charge expenses are recorded from the invoice. '
                'Pick another category.')
        return value

    def validate(self, attrs):
        if self.instance is not None and self.instance.is_invoice_charge:
            raise serializers.ValidationError({
                'detail': 'This expense is an extra charge on a sales invoice. '
                          'Change the charge on the invoice instead.'
            })
        return super().validate(attrs)

    def _transaction(self, expense):
        return getattr(expense, 'transaction_record', None)

    def get_paid_from(self, expense):
        transaction = self._transaction(expense)
        return transaction.account_id if transaction else None

    def get_paid_on(self, expense):
        transaction = self._transaction(expense)
        return transaction.date if transaction else None

    def get_paid_from_name(self, expense):
        transaction = self._transaction(expense)
        account = transaction.account if transaction else None
        return account.name if account else None

    def get_expense_date(self, expense):
        return expense.get_expense_date()

    class Meta:
        model = Expense
        fields = [
            'id', 'business', 'name', 'category', 'desc',
            'amount', 'created_at', 'account', 'date',
            'paid_from', 'paid_on', 'paid_from_name', 'expense_date',
            'is_invoice_charge', 'sales_invoice',
        ]

    def create(self, validated_data):
        money_details = self._pop_money_details(validated_data)
        expense = Expense.objects.create(
            business_id=self.context['business_id'], **validated_data)
        self._apply_money_details(expense, money_details)
        return expense

    def update(self, instance, validated_data):
        money_details = self._pop_money_details(validated_data)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        self._apply_money_details(instance, money_details)
        return instance

    def _pop_money_details(self, validated_data):
        return {
            'account_id': validated_data.pop('account', None),
            'date': validated_data.pop('date', None),
        }

    def _apply_money_details(self, expense, money_details):
        if not any(money_details.values()):
            return

        # No try/except. An expense whose mirrored transaction is missing means
        # money left the books on paper but not in any account; swallowing that
        # reported success for a half-written record.
        transaction = expense.transaction_record

        fields = []
        for attr, value in money_details.items():
            if value:
                setattr(transaction, attr, value)
                fields.append(attr)

        if fields:
            transaction.save(update_fields=fields)


# ── Employee ──────────────────────────────────────────────────────────────────

class SimpleUserSerializer(serializers.ModelSerializer):

    class Meta:
        model = User
        # role: the Employees page tells data entry accounts apart.
        fields = ['id', 'email', 'first_name', 'last_name', 'role']
        read_only_fields = ['role']


class SimpleEmployeeAccessSerializer(serializers.ModelSerializer):

    class Meta:
        model = EmployeeAccess
        fields = ['id', 'permissions']

class EmployeeSerializer(serializers.ModelSerializer):

    user = SimpleUserSerializer(read_only=True)
    business = SimpleBusinessSerializer(read_only=True)
    created_by = SimpleUserSerializer(read_only=True)
    access = SimpleEmployeeAccessSerializer()

    class Meta:
        model = Employee
        fields = ['id', 'user', 'business', 'created_by', 'created_at', 'updated_at', 'access']


class EmployeeCreateSerializer(serializers.ModelSerializer):
    
    user = SimpleUserSerializer(read_only=True)
    created_by = SimpleUserSerializer(read_only=True)

    class Meta:
        model = Employee
        fields = ['user', 'business', 'created_by', 'created_at', 'updated_at']

class EmployeeAndUserCreateSerializer(serializers.ModelSerializer):

    first_name = serializers.CharField(max_length=256)
    last_name = serializers.CharField(max_length=256)
    email = serializers.EmailField()
    password = serializers.CharField(max_length=256)
    re_password = serializers.CharField(max_length=256)
    permissions = serializers.DictField(required=False, default=dict)
    # 'employee', or 'backlog' for a data entry account. Set explicitly:
    # left to the model default, every employee came out an 'admin'.
    role = serializers.ChoiceField(
        choices=[User.EMPLOYEE, User.BACKLOG], required=False, default=User.EMPLOYEE)

    class Meta:
        model = Employee
        fields = ['first_name', 'last_name', 'email', 'password', 're_password', 'permissions', 'role']

    def create(self, validated_data):

        try:
            with transaction.atomic():
                email = validated_data.pop('email')
                permissions = validated_data.pop('permissions')
                role = validated_data.pop('role', User.EMPLOYEE)
                # Data entry has no module permissions; the backlog role is
                # its whole access.
                if role == User.BACKLOG:
                    permissions = {}

                user = User.objects.create_user(
                    email,
                    password=validated_data.pop('password'),
                    first_name=validated_data.pop('first_name'),
                    last_name=validated_data.pop('last_name'),
                    role=role,
                )

                employee = Employee.objects.create(
                    user=user,
                    business_id=self.context['business_id'],
                    created_by_id=self.context['created_by_id'],
                )

                permissions = EmployeeAccess.objects.create(
                    employee = employee,
                    permissions = permissions
                )

                return employee
        except Exception as error:
            # Re-raised, not swallowed: returning None here turned a
            # failed write into a misleading 400 at the view.
            print(error)
            raise
        

class EmployeeUpdateSerializer(serializers.ModelSerializer):

    access = SimpleEmployeeAccessSerializer()
    class Meta:
        model = Employee
        fields = ['access']

    def update(self, instance, validated_data):
        access = instance.access
        payload_access = validated_data.pop('access')

        for attr, value in payload_access.items():
            setattr(access, attr, value)
        access.save()
        instance.refresh_from_db()
        return instance


# ── EmployeeAccess ────────────────────────────────────────────────────────────

class EmployeeAccessSerializer(serializers.ModelSerializer):

    class Meta:
        model = EmployeeAccess
        fields = ['id', 'employee', 'permissions']


class EmployeeAccessCreateSerializer(serializers.ModelSerializer):

    class Meta:
        model = EmployeeAccess
        fields = ['permissions']

    def create(self, validated_data):

        employee_id=self.context['employee_id']

        return EmployeeAccess.objects.create(
            employee_id= employee_id,
            **validated_data
        )


class EmployeeAccessUpdateSerializer(serializers.ModelSerializer):

    class Meta:
        model = EmployeeAccess
        fields = ['permissions']

    def update(self, instance, validated_data):
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        return instance

