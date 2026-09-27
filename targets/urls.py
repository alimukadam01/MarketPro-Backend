from rest_framework_nested.routers import DefaultRouter

from .views import ManualDataPointViewSet, TargetKPIViewSet, TargetViewSet

router = DefaultRouter()
router.register('targets', TargetViewSet, basename='targets')
router.register('manual-data-points', ManualDataPointViewSet,
                basename='manual-data-points')
router.register('target-kpis', TargetKPIViewSet, basename='target-kpis')

urlpatterns = router.urls
