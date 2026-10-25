from rest_framework_nested.routers import DefaultRouter

from .views import BacklogEntryViewSet, GlossarySuggestionViewSet, GlossaryViewSet, UploadBatchViewSet
from .work_views import BacklogWorkViewSet

router = DefaultRouter()
router.register('backlog-entries', BacklogEntryViewSet, basename='backlog-entries')
router.register('backlog-batches', UploadBatchViewSet, basename='backlog-batches')
router.register('backlog-glossary', GlossaryViewSet, basename='backlog-glossary')
router.register('backlog-glossary-suggestions', GlossarySuggestionViewSet,
                basename='backlog-glossary-suggestions')
router.register('backlog-work', BacklogWorkViewSet, basename='backlog-work')

urlpatterns = router.urls
