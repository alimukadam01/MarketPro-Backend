from rest_framework import serializers
from django.db import transaction
from root.serializers import SimpleBusinessSerializer, SimpleCustomerSerializer
from sales.models import PurchaseQuotation, PurchaseQuotationItem, SalesInvoice, PurchaseInvoice
from sales.serializers import PurchaseInvoiceSerializer, PurchaseQuotationSerializer, SalesInvoiceSerializer, SimpleSalesInvoiceSerializer, SimplePurchaseInvoiceSerializer
from .models import Project, ProjectPurchaseInvoice, ProjectPurchaseQuotation, ProjectSalesInvoice, ProjectTask


class ProjectTaskSerializer(serializers.ModelSerializer):

    class Meta:
        model = ProjectTask
        fields = ['id', 'name', 'start', 'end', 'description', 'is_complete']


class ProjectTaskCreateSerializer(serializers.ModelSerializer):

    def save(self, **kwargs):
        return ProjectTask.objects.create(
            project_id=self.context['project_id'],
            **self.validated_data
        )
    class Meta:
        model = ProjectTask
        fields = ['id', 'name', 'start', 'end', 'description']


class SimpleProjectSerializer(serializers.ModelSerializer):
    customer = SimpleCustomerSerializer(read_only=True)
    num_purchase_inv = serializers.SerializerMethodField()
    num_sales_inv = serializers.SerializerMethodField()
    num_tasks = serializers.SerializerMethodField()

    def get_num_purchase_inv(self, obj):
        return obj.purchase_invoices.count()

    def get_num_sales_inv(self, obj):
        return obj.sales_invoices.count()

    def get_num_tasks(self, obj):
        return obj.tasks.count()

    class Meta:
        model = Project
        fields = [
            'id', 'name', 'customer', 'status',
            'desc', 'created_at', 'updated_at',
            'num_sales_inv', 'num_purchase_inv',
            'num_tasks'
        ]


class ProjectPurchaseQuotationSerializer(serializers.ModelSerializer):

    purchase_quotation = PurchaseQuotationSerializer(read_only=True)
    class Meta:
        model = ProjectPurchaseQuotation
        fields = ['id', 'project', 'purchase_quotation']        


class ProjectCreateSerializer(serializers.ModelSerializer):

    def save(self, **kwargs):

        return Project.objects.create(
            business_id=self.context['business_id'],
            **self.validated_data
        )

    class Meta:
        model = Project
        fields = [
            'id', 'name', 'customer', 'status', 'desc'
        ]


class ProjectUpdateSerializer(serializers.ModelSerializer):
    pass


class ProjectSalesInvoiceCreateSerializer(serializers.ModelSerializer):

    sales_invoice = serializers.PrimaryKeyRelatedField(
        queryset=SalesInvoice.objects.none())

    def save(self, **kwargs):
        return ProjectSalesInvoice.objects.create(
            project_id=self.context['project_id'],
            **self.validated_data
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        business_id = self.context.get('business_id')
        if business_id:
            self.fields['sales_invoice'].queryset = SalesInvoice.objects.filter(
                business_id=business_id
            )

    class Meta:
        model = ProjectSalesInvoice
        fields = [
            'id', 'sales_invoice'
        ]


class ProjectSalesInvoiceSerializer(ProjectSalesInvoiceCreateSerializer):

    sales_invoice = SimpleSalesInvoiceSerializer(read_only=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class ProjectSalesInvoiceDetailSerializer(ProjectSalesInvoiceSerializer):
    sales_invoice = SalesInvoiceSerializer(read_only=True)


class ProjectPurchaseInvoiceCreateSerializer(serializers.ModelSerializer):

    purchase_invoice = serializers.PrimaryKeyRelatedField(
        queryset=PurchaseInvoice.objects.none())

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        business_id = self.context.get('business_id')
        if business_id:
            self.fields['purchase_invoice'].queryset = PurchaseInvoice.objects.filter(
                business_id=business_id
            )

    def save(self, **kwargs):
        return ProjectPurchaseInvoice.objects.create(
            project_id=self.context['project_id'],
            **self.validated_data
        )

    class Meta:
        model = ProjectPurchaseInvoice
        fields = [
            'id', 'purchase_invoice'
        ]


class ProjectPurchaseInvoiceSerializer(ProjectPurchaseInvoiceCreateSerializer):

    purchase_invoice = SimplePurchaseInvoiceSerializer(read_only=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class ProjectPurchaseInvoiceDetailSerializer(ProjectPurchaseInvoiceSerializer):

    purchase_invoice = PurchaseInvoiceSerializer(read_only=True)


class ProjectPurchaseQuotationCreateSerializer(serializers.ModelSerializer):
    purchase_quotation = serializers.PrimaryKeyRelatedField(
        queryset=PurchaseQuotation.objects.none())

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['purchase_quotation'].queryset = PurchaseQuotation.objects.filter(
            business_id=self.context['business_id']
        )


class CompleteProjectCreateSerializer(serializers.ModelSerializer):

    tasks = serializers.ListField()
    quotations = serializers.ListField()

    class Meta:
        model = Project
        fields = [
            'id', 'name', 'customer', 'desc', 'status', 'tasks', 'quotations'
        ]

    def save(self, **kwargs):

        try:
            with transaction.atomic():

                tasks = self.validated_data.pop('tasks')
                quotations = self.validated_data.pop('quotations')

                project = Project.objects.create(
                    business_id=self.context['business_id'],
                    **self.validated_data
                )

                # create project task objects
                tasks_tbc = []
                for task in tasks:
                    tasks_tbc.append(ProjectTask(
                        project=project,
                        **task
                    ))

                # create project purchase quotation and items objects
                quotation_tbc = quotations[0]
                quotation_items = quotation_tbc.pop('items')
                quotation = PurchaseQuotation.objects.create(
                    business_id=self.context['business_id'],
                    **quotation_tbc
                )

                quotation_items_tbc = [PurchaseQuotationItem(
                    business_id=self.context['business_id'],
                    purchase_quotation=quotation,
                    **quotation_item
                ) for quotation_item in quotation_items]

                ProjectTask.objects.bulk_create(tasks_tbc)
                PurchaseQuotationItem.objects.bulk_create(quotation_items_tbc)

                # link quotation to project
                ProjectPurchaseQuotation.objects.create(
                    project=project,
                    purchase_quotation=quotation
                )

                project.refresh_from_db()
                return project

        except Exception as error:
            print(error)


class ProjectSerializer(serializers.ModelSerializer):

    sales_invoices = ProjectSalesInvoiceSerializer(many=True, read_only=True)
    purchase_invoices = ProjectPurchaseInvoiceSerializer(many=True, read_only=True)
    purchase_quotations = ProjectPurchaseQuotationSerializer(many=True, read_only=True)
    tasks = ProjectTaskSerializer(many=True, read_only=True)
    class Meta:
        model = Project
        fields = [
            'id', 'name', 'customer', 'status',
            'desc', 'created_at', 'updated_at',
            'sales_invoices', 'purchase_invoices',
            'purchase_quotations', 'tasks'
        ]


class ProjectDetailSerializer(ProjectSerializer):
    sales_invoices = ProjectSalesInvoiceDetailSerializer(read_only=True)
    purchase_invoices = ProjectPurchaseInvoiceDetailSerializer(read_only=True)