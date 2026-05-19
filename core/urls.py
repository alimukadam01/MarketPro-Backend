from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import AccessConfigView

urlpatterns = [
    path('auth/', include('djoser.urls')),
    path('auth/', include('djoser.urls.jwt')),
    path('auth/access-config/', AccessConfigView.as_view()),
]
