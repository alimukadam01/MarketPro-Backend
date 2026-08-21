from rest_framework_nested.routers import DefaultRouter

from .views import (
    AccountingKPIViewSet, MoneyAccountViewSet, PartyOpeningBalanceViewSet,
    TransactionViewSet,
)

router = DefaultRouter()
router.register('money-accounts', MoneyAccountViewSet,
                basename='money-accounts')
router.register('transactions', TransactionViewSet, basename='transactions')
router.register('party-opening-balances', PartyOpeningBalanceViewSet,
                basename='party-opening-balances')
router.register('accounting-kpis', AccountingKPIViewSet,
                basename='accounting-kpis')

urlpatterns = router.urls
