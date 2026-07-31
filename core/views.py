from datetime import datetime

from django.contrib.auth import get_user_model
from rest_framework.views import APIView
from rest_framework.viewsets import ViewSet
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.decorators import action
from rest_framework import status

from root.models import Business, EmployeeAccess
from .access_config import UserAccessConfig
from .utils import send_marketpro_email

User = get_user_model()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_business_for_user(user, business_id):
    """
    Returns the Business object if the user has access to it.
    Access means: they own it (admin) OR have an EmployeeAccess for it.
    Returns None if no access.
    """
    try:
        business = Business.objects.get(pk=business_id)
    except Business.DoesNotExist:
        return None

    if business.owner_id == user.id:
        return business

    if EmployeeAccess.objects.filter(user=user, business=business).exists():
        return business

    return None


class IsAdminRole(IsAuthenticated):
    """Allows access only to users with role='admin'."""

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        return request.user.role == User.ADMIN


# ---------------------------------------------------------------------------
# GET /auth/access-config/?business_id=<id>
# ---------------------------------------------------------------------------

class AccessConfigView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        business_id = request.query_params.get('business_id')
        if not business_id:
            return Response(
                {'detail': 'business_id query param is required.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        business = _get_business_for_user(request.user, business_id)
        if not business:
            return Response(
                {'detail': 'You do not have access to this business.'},
                status=status.HTTP_403_FORBIDDEN
            )

        config = UserAccessConfig(request.user, business)
        return Response(config.to_dict())


# ---------------------------------------------------------------------------
# POST /contact/   (public — no auth required)
# ---------------------------------------------------------------------------

ADMIN_EMAIL = 'admin@market-pro.pk'


class LandingPageContactView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        data = request.data
        if not data:
            return Response({'detail': 'Bad Request.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            send_marketpro_email(
                subject='New Landing Page Submission',
                to_email=ADMIN_EMAIL,
                template_name='emails/landing_contact.html',
                context={
                    'data': data,
                    'submitted_at': datetime.now().strftime('%d %b %Y, %I:%M %p'),
                    'year': datetime.now().year,
                },
            )
            return Response({'detail': 'Message sent successfully.'}, status=status.HTTP_200_OK)
        except Exception as error:
            print(error)
            return Response({'detail': 'Internal Server Error.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
