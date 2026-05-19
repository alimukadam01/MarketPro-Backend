from rest_framework import status
from rest_framework.viewsets import ModelViewSet
from rest_framework.response import Response
from rest_framework.decorators import action

from projects.serializers import CompleteProjectCreateSerializer, ProjectCreateSerializer, ProjectPurchaseInvoiceCreateSerializer, ProjectPurchaseInvoiceDetailSerializer, ProjectPurchaseInvoiceSerializer, ProjectSalesInvoiceCreateSerializer, ProjectSalesInvoiceDetailSerializer, ProjectSalesInvoiceSerializer, ProjectSerializer, ProjectTaskCreateSerializer, ProjectTaskSerializer, SimpleProjectSerializer
from projects.models import Project, ProjectPurchaseInvoice, ProjectSalesInvoice, ProjectTask
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
        if self.action == "list":
            return SimpleProjectSerializer

        if self.action == "complete_creation":
            return CompleteProjectCreateSerializer

        if method == 'POST':
            return ProjectCreateSerializer
        return ProjectSerializer

    def get_serializer_context(self):

        business = get_active_business(self.request)
        if not business:
            return {}

        return {
            'business_id': business.id
        }

    @action(['POST'], detail=False, url_path='bulk-delete', url_name='bulk-delete')
    def bulk_delete(self, request):
        project_ids = request.data.get('project_ids', [])
        if not project_ids:
            return Response({
                'detail': 'Bad Request.'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            Project.objects.filter(id__in=project_ids).delete()
            return Response({
                'detail': 'Success.'
            }, status=status.HTTP_200_OK)

        except Exception as error:
            print(error)
            return Response({
                'detail': 'Internal Server Error.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(['POST'], detail=False, url_path='complete-creation', url_name='complete-creation')
    def complete_creation(self, request):
        try:
            business = get_active_business(request)
            if not business:
                return Response({
                    'detail': 'No active business exists. Please contact admin.'
                }, status=status.HTTP_400_BAD_REQUEST)
            
            serializer = CompleteProjectCreateSerializer(
                data=request.data,
                context={
                    'business_id': business.id
                }
            )
            serializer.is_valid(raise_exception=True)
            project = serializer.save()

            if not project:
                return Response({
                    'detail': 'Bad Request'
                }, status=status.HTTP_400_BAD_REQUEST)

            serializer = ProjectSerializer(project)
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        except Exception as error:
            print(error)
            return Response({
                "detail": "Internal Server Error"
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class ProjectTaskViewSet(ModelViewSet):

    def get_queryset(self):
        return ProjectTask.objects.filter(project_id=self.kwargs['project_pk'])

    def get_serializer_class(self):

        method = self.request.method
        if method == 'POST':
            return ProjectTaskCreateSerializer

        return ProjectTaskSerializer

    def get_serializer_context(self):
        return {
            'project_id': self.kwargs['project_pk']
        }

    @action(methods=['GET'], detail=True, url_path='toggle-completion', url_name='toggle-completion')
    def toggle_completion(self, request, project_pk=None, pk=None):

        try:
            object = self.get_object()
            object.is_complete = not object.is_complete
            object.save()

            return Response({
                "detail": "OK"
            }, status=status.HTTP_200_OK)
        except Exception as error:
            print(error)
            return Response({
                "detail": "Internal Server Error"
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class ProjectSalesInvoiceViewSet(ModelViewSet):

    def get_queryset(self):
        project_id = self.kwargs.get('project_pk')
        if not project_id:
            return []

        return ProjectSalesInvoice.objects.filter(project_id=project_id)

    def get_serializer_class(self):

        if self.action == 'retrieve':
            return ProjectSalesInvoiceDetailSerializer

        if self.request.method == 'POST':
            return ProjectSalesInvoiceCreateSerializer

        return ProjectSalesInvoiceSerializer

    def get_serializer_context(self):
        business = get_active_business(self.request)

        return {
            'project_id': self.kwargs.get('project_pk'),
            'business_id': business.id if business else None
        }


class ProjectPurchaseInvoiceViewSet(ModelViewSet):

    def get_queryset(self):
        project_id = self.kwargs.get('project_pk')
        if not project_id:
            return []

        return ProjectPurchaseInvoice.objects.filter(project_id=project_id)

    def get_serializer_class(self):

        if self.action == 'retrieve':
            return ProjectPurchaseInvoiceDetailSerializer

        if self.request.method == 'POST':
            return ProjectPurchaseInvoiceCreateSerializer
        return ProjectPurchaseInvoiceSerializer

    def get_serializer_context(self):
        business = get_active_business(self.request)
        if not business:
            return {}

        return {
            'project_id': self.kwargs.get('project_pk'),
            'business_id': business.id
        }
