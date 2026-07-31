from rest_framework_nested.routers import DefaultRouter
from .views import BacklogEntryViewSet

router = DefaultRouter()
router.register('backlog-entries', BacklogEntryViewSet, basename='backlog-entries')

urlpatterns = router.urls
