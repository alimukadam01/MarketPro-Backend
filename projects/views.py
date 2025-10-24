from rest_framework.viewsets import ModelViewSet

from projects.serializers import ProjectCreateSerializer, ProjectPurchaseInvoiceCreateSerializer, ProjectPurchaseInvoiceSerializer, ProjectSalesInvoiceCreateSerializer, ProjectSalesInvoiceSerializer, ProjectSerializer
from projects.models import Project, ProjectPurchaseInvoice, ProjectSalesInvoice
from root.utils import get_active_business

# Create your views here.

class ProjectViewSet(ModelViewSet):

    def get_queryset(self):

        business = get_active_business(self.request)
        if not business:
            return []
        
        return Project.objects.filter(business_id=business.id)
    
    def get_serializer_class(self):
        method = self.request.method

        if method == 'POST':
            return ProjectCreateSerializer
        return ProjectSerializer

    def get_serializer_context(self):
        return {
            'business_id': get_active_business(self.request).id    
        }
    

class ProjectSalesInvoiceViewSet(ModelViewSet):
    
    def get_queryset(self):
        project_id = self.kwargs.get('project_pk')
        if not project_id:
            return []
        
        return ProjectSalesInvoice.objects.filter(project_id=project_id)
    
    def get_serializer_class(self):
        
        if self.request.method == 'POST':
            return ProjectSalesInvoiceCreateSerializer
        return ProjectSalesInvoiceSerializer
    
    def get_serializer_context(self):
        business = get_active_business(self.request)

        return {
            'project_id': self.kwargs.get('project_pk'),
            'busieness_id': business.id if business else None
        }


class ProjectPurchaseInvoiceViewSet(ModelViewSet):
    
    def get_queryset(self):
        project_id = self.kwargs.get('project_pk')
        if not project_id:
            return []
        
        return ProjectPurchaseInvoice.objects.filter(project_id=project_id)
    
    def get_serializer_class(self):
        if self.request.method == 'POST':
            return ProjectPurchaseInvoiceCreateSerializer
        return ProjectPurchaseInvoiceSerializer
    
    def get_serializer_context(self):
        business = get_active_business(self.request)

        return {
            'project_id': self.kwargs.get('project_pk'),
            'business_id': business.id if business else None
        }