from rest_framework import serializers
from root.serializers import SimpleBusinessSerializer
from sales.models import SalesInvoice, PurchaseInvoice
from sales.serializers import SimpleSalesInvoiceSerializer, SimplePurchaseInvoiceSerializer 
from .models import Project, ProjectPurchaseInvoice, ProjectSalesInvoice

class ProjectSerializer(serializers.ModelSerializer):

    business = SimpleBusinessSerializer(read_only=True)
    sales_invoices = SimpleSalesInvoiceSerializer(many=True, read_only=True)
    purchase_invoices = SimplePurchaseInvoiceSerializer(many=True, read_only=True)

    class Meta:
        model = Project
        fields = [
            'id', 'title', 'business', 'customer', 'status',
            'description', 'created_at', 'updated_at',
            'sales_invoices', 'purchase_invoices'
        ]


class ProjectCreateSerializer(serializers.ModelSerializer):

    def save(self, **kwargs):

        return Project.objects.create(
            business_id=self.context['business_id'],
            **self.validated_data
        )

    class Meta:
        model = Project
        fields = [
            'id', 'title', 'customer', 'status', 'description'
        ]


class ProjectUpdateSerializer(serializers.ModelSerializer):
    pass


class ProjectSalesInvoiceCreateSerializer(serializers.ModelSerializer):

    def save(self, **kwargs):
        return ProjectSalesInvoice.objects.create(
            project_id=self.context['project_id'],
            **self.validated_data
        )

    class Meta:
        model = ProjectSalesInvoice
        fields = [
            'sales_invoice'
        ]


class ProjectSalesInvoiceSerializer(ProjectSalesInvoiceCreateSerializer):

    sales_invoice = SimpleSalesInvoiceSerializer(read_only=True)


class ProjectPurchaseInvoiceCreateSerializer(serializers.ModelSerializer):

    purchase_invoice = serializers.PrimaryKeyRelatedField(queryset=PurchaseInvoice.objects.none())

    def __init__(self, instance=None, data=..., **kwargs):
        super().__init__(instance, data, **kwargs)
        self.fields['purchase_invoice'].queryset = PurchaseInvoice.objects.filter(
            business_id=self.context['business_id']
        )

    def save(self, **kwargs):
        return ProjectPurchaseInvoice.objects.create(
            project_id=self.context['project_id'],
            **self.validated_data
        )

    class Meta:
        model = ProjectPurchaseInvoice
        fields = [
            'purchase_invoice'
        ]


class ProjectPurchaseInvoiceSerializer(ProjectPurchaseInvoiceCreateSerializer):

    purchase_invoice = SimplePurchaseInvoiceSerializer(read_only=True)