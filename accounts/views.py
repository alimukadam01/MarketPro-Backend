from django.db import transaction as db_transaction
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.filters import SearchFilter
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet, ModelViewSet

from root.models import Customer, Supplier
from root.utils import get_active_business, whatsapp_number
from .models import MoneyAccount, PartyOpeningBalance, Transaction
from .serializers import (
    MoneyAccountCreateSerializer, MoneyAccountSerializer,
    MoneyAccountUpdateSerializer, PartyOpeningBalanceSerializer,
    SimpleMoneyAccountSerializer, SimpleTransactionSerializer,
    TransactionCreateSerializer, TransactionSerializer,
    TransactionUpdateSerializer,
)
from .utils import (
    daily_summary, day_book, has_accounting_access, month_bounds, parse_date,
    party_ledger, payables, previous_month_bounds, profit_estimate,
    receivables,
)


NO_BUSINESS = {'detail': 'No active business exists. Please contact admin.'}
NO_ACCESS = {'detail': 'You do not have access to accounting.'}


class MoneyAccountViewSet(ModelViewSet):

    filter_backends = [SearchFilter, DjangoFilterBackend]
    filterset_fields = ['type', 'is_active']
    search_fields = ['id', 'name', 'type']

    def get_queryset(self):
        business = get_active_business(self.request)
        if not business:
            return []
        if not has_accounting_access(self.request, business):
            return []
        return MoneyAccount.objects.filter(
            business_id=business.id).order_by('-is_default', 'name')

    def get_serializer_class(self):
        method = self.request.method
        if self.action == 'list':
            return SimpleMoneyAccountSerializer
        if method == 'POST':
            return MoneyAccountCreateSerializer
        if method in ('PUT', 'PATCH'):
            return MoneyAccountUpdateSerializer
        return MoneyAccountSerializer

    def get_serializer_context(self):
        business = get_active_business(self.request)
        if not business:
            return {}
        return {'business_id': business.id}

    def destroy(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            if instance.is_system:
                return Response(
                    {'detail': 'The account the business was created with cannot be deleted.'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            if instance.has_transactions():
                return Response(
                    {'detail': 'Account carries transactions. Deactivate it instead.'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            instance.delete()
            return Response({'detail': 'Success.'}, status=status.HTTP_200_OK)
        except Exception as error:
            print(error)
            return Response(
                {'detail': 'Internal Server Error.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(['POST'], detail=True, url_path='toggle-active', url_name='toggle-active')
    def toggle_active(self, request, pk=None):
        business = get_active_business(request)
        if not business:
            return Response(NO_BUSINESS, status=status.HTTP_400_BAD_REQUEST)
        if not has_accounting_access(request, business):
            return Response(NO_ACCESS, status=status.HTTP_403_FORBIDDEN)

        try:
            instance = self.get_object()

            if instance.is_active and instance.is_system:
                return Response(
                    {'detail': 'The account the business was created with cannot be deactivated.'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            with db_transaction.atomic():
                instance.is_active = not instance.is_active
                fields = ['is_active']

                # Deactivating the default hands the role back to the account
                # the business was created with.
                if not instance.is_active and instance.is_default:
                    instance.is_default = False
                    fields.append('is_default')
                    MoneyAccount.objects.filter(
                        id=MoneyAccount.objects.system_account_id(business.id)
                    ).update(is_default=True)

                instance.save(update_fields=fields)

            return Response(
                {'detail': 'OK', 'is_active': instance.is_active},
                status=status.HTTP_200_OK
            )
        except Exception as error:
            print(error)
            return Response(
                {'detail': 'Internal Server Error.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(['POST'], detail=True, url_path='set-default', url_name='set-default')
    def set_default(self, request, pk=None):
        business = get_active_business(request)
        if not business:
            return Response(NO_BUSINESS, status=status.HTTP_400_BAD_REQUEST)
        if not has_accounting_access(request, business):
            return Response(NO_ACCESS, status=status.HTTP_403_FORBIDDEN)

        try:
            instance = self.get_object()

            if not instance.is_active:
                return Response(
                    {'detail': 'A deactivated account cannot be the default.'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Exactly one default at a time.
            with db_transaction.atomic():
                MoneyAccount.objects.filter(
                    business_id=business.id, is_default=True
                ).exclude(id=instance.id).update(is_default=False)

                if not instance.is_default:
                    instance.is_default = True
                    instance.save(update_fields=['is_default'])

            return Response({'detail': 'OK'}, status=status.HTTP_200_OK)
        except Exception as error:
            print(error)
            return Response(
                {'detail': 'Internal Server Error.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(['POST'], detail=False, url_path='bulk-delete', url_name='bulk-delete')
    def bulk_delete(self, request):
        business = get_active_business(request)
        if not business:
            return Response(NO_BUSINESS, status=status.HTTP_400_BAD_REQUEST)

        account_ids = request.data.get('account_ids', [])
        if not account_ids:
            return Response(
                {'detail': 'Bad Request.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            accounts = MoneyAccount.objects.filter(
                id__in=account_ids, business_id=business.id)

            for account in accounts:
                if account.is_system:
                    return Response(
                        {'detail': 'The account the business was created with cannot be deleted.'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                if account.has_transactions():
                    return Response(
                        {'detail': 'One or more accounts carry transactions. Deactivate them instead.'},
                        status=status.HTTP_400_BAD_REQUEST
                    )

            accounts.delete()
            return Response({'detail': 'Success.'}, status=status.HTTP_200_OK)
        except Exception as error:
            print(error)
            return Response(
                {'detail': 'Internal Server Error.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class TransactionViewSet(ModelViewSet):

    filter_backends = [SearchFilter, DjangoFilterBackend]
    filterset_fields = ['type', 'status', 'payment_method', 'account', 'date']
    search_fields = ['id', 'reference', 'notes', 'type']

    def get_queryset(self):
        business = get_active_business(self.request)
        if not business:
            return []
        if not has_accounting_access(self.request, business):
            return []
        return Transaction.objects.filter(
            business_id=business.id
        ).select_related('account', 'transfer_account')

    def get_serializer_class(self):
        method = self.request.method
        if self.action == 'list':
            return SimpleTransactionSerializer
        if method == 'POST':
            return TransactionCreateSerializer
        if method in ('PUT', 'PATCH'):
            return TransactionUpdateSerializer
        return TransactionSerializer

    def get_serializer_context(self):
        business = get_active_business(self.request)
        if not business:
            return {}
        return {
            'business_id': business.id,
            'user_id': self.request.user.id,
        }

    def destroy(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            if instance.is_source_linked:
                return Response(
                    {'detail': 'Delete the source payment or expense instead.'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            instance.delete()
            return Response({'detail': 'Success.'}, status=status.HTTP_200_OK)
        except Exception as error:
            print(error)
            return Response(
                {'detail': 'Internal Server Error.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(['GET'], detail=False, url_path='pending-cheques', url_name='pending-cheques')
    def pending_cheques(self, request):
        business = get_active_business(request)
        if not business:
            return Response(NO_BUSINESS, status=status.HTTP_400_BAD_REQUEST)
        if not has_accounting_access(request, business):
            return Response(NO_ACCESS, status=status.HTTP_403_FORBIDDEN)

        try:
            cheques = Transaction.objects.pending_cheques(business.id)
            serializer = SimpleTransactionSerializer(cheques, many=True)
            return Response(serializer.data, status=status.HTTP_200_OK)
        except Exception as error:
            print(error)
            return Response(
                {'detail': 'Internal Server Error.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(['POST'], detail=True, url_path='mark-cleared', url_name='mark-cleared')
    def mark_cleared(self, request, pk=None):
        return self._set_status(request, 'C')

    @action(['POST'], detail=True, url_path='mark-bounced', url_name='mark-bounced')
    def mark_bounced(self, request, pk=None):
        return self._set_status(request, 'B')

    def _set_status(self, request, new_status):
        business = get_active_business(request)
        if not business:
            return Response(NO_BUSINESS, status=status.HTTP_400_BAD_REQUEST)
        if not has_accounting_access(request, business):
            return Response(NO_ACCESS, status=status.HTTP_403_FORBIDDEN)

        try:
            instance = self.get_object()
            instance.status = new_status
            instance.save(update_fields=['status'])
            return Response(
                {'detail': 'OK', 'status': instance.status},
                status=status.HTTP_200_OK
            )
        except Exception as error:
            print(error)
            return Response(
                {'detail': 'Internal Server Error.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(['POST'], detail=False, url_path='bulk-delete', url_name='bulk-delete')
    def bulk_delete(self, request):
        business = get_active_business(request)
        if not business:
            return Response(NO_BUSINESS, status=status.HTTP_400_BAD_REQUEST)

        transaction_ids = request.data.get('transaction_ids', [])
        if not transaction_ids:
            return Response(
                {'detail': 'Bad Request.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            Transaction.objects.filter(
                id__in=transaction_ids,
                business_id=business.id,
                sales_receipt__isnull=True,
                purchase_receipt__isnull=True,
                expense__isnull=True,
            ).delete()
            return Response({'detail': 'Success.'}, status=status.HTTP_200_OK)
        except Exception as error:
            print(error)
            return Response(
                {'detail': 'Internal Server Error.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class PartyOpeningBalanceViewSet(ModelViewSet):

    serializer_class = PartyOpeningBalanceSerializer
    filter_backends = [SearchFilter, DjangoFilterBackend]
    filterset_fields = ['customer', 'supplier']
    search_fields = ['id', 'amount']

    def get_queryset(self):
        business = get_active_business(self.request)
        if not business:
            return []
        if not has_accounting_access(self.request, business):
            return []
        return PartyOpeningBalance.objects.filter(
            business_id=business.id).order_by('-created_at')

    def get_serializer_context(self):
        business = get_active_business(self.request)
        if not business:
            return {}
        return {'business_id': business.id}


class AccountingKPIViewSet(GenericViewSet):

    queryset = []
    serializer_class = None

    def _guard(self, request):
        """
        Returns (business, error_response). Exactly one of the two is None.
        """
        business = get_active_business(request)
        if not business:
            return None, Response(
                NO_BUSINESS, status=status.HTTP_400_BAD_REQUEST)
        if not has_accounting_access(request, business):
            return None, Response(NO_ACCESS, status=status.HTTP_403_FORBIDDEN)
        return business, None

    @action(['GET'], detail=False, url_path='cash-in-hand', url_name='cash-in-hand')
    def cash_in_hand(self, request):
        business, guard_error = self._guard(request)
        if guard_error:
            return guard_error

        as_of = parse_date(request.query_params.get('as_of'))
        try:
            data = MoneyAccount.objects.cash_in_hand(business.id, as_of)
            return Response({'cash_in_hand': data}, status=status.HTTP_200_OK)
        except Exception as error:
            print(error)
            return Response(
                {'detail': 'Internal Server Error.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(['GET'], detail=False, url_path='receivables', url_name='receivables')
    def receivables(self, request):
        business, guard_error = self._guard(request)
        if guard_error:
            return guard_error

        try:
            return Response(
                {'receivables': receivables(business.id)},
                status=status.HTTP_200_OK
            )
        except Exception as error:
            print(error)
            return Response(
                {'detail': 'Internal Server Error.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(['GET'], detail=False, url_path='payables', url_name='payables')
    def payables(self, request):
        business, guard_error = self._guard(request)
        if guard_error:
            return guard_error

        try:
            return Response(
                {'payables': payables(business.id)},
                status=status.HTTP_200_OK
            )
        except Exception as error:
            print(error)
            return Response(
                {'detail': 'Internal Server Error.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(['GET'], detail=False, url_path='party-ledger', url_name='party-ledger')
    def party_ledger(self, request):
        business, guard_error = self._guard(request)
        if guard_error:
            return guard_error

        customer_id = request.query_params.get('customer_id')
        supplier_id = request.query_params.get('supplier_id')

        if not customer_id and not supplier_id:
            return Response(
                {'detail': 'Bad Request.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            customer = supplier = None
            if customer_id:
                customer = Customer.objects.filter(
                    id=customer_id, business_id=business.id).first()
            else:
                supplier = Supplier.objects.filter(
                    id=supplier_id, business_id=business.id).first()

            if not customer and not supplier:
                return Response(
                    {'detail': 'Not Found'}, status=status.HTTP_404_NOT_FOUND)

            ledger = party_ledger(
                business.id,
                customer=customer,
                supplier=supplier,
                date_from=parse_date(request.query_params.get('date_from')),
                date_to=parse_date(request.query_params.get('date_to')),
            )
            party = customer or supplier
            ledger['party'] = {
                'id': party.id,
                'name': party.name,
                'phone': party.phone,
                # Numbers are stored as they are dialled locally, which no
                # wa.me link can use. Normalised here so the rule lives in
                # one place.
                'whatsapp': whatsapp_number(party.phone),
                'is_customer': bool(customer),
            }
            return Response({'party_ledger': ledger}, status=status.HTTP_200_OK)
        except Exception as error:
            print(error)
            return Response(
                {'detail': 'Internal Server Error.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(['GET'], detail=False, url_path='day-book', url_name='day-book')
    def day_book(self, request):
        business, guard_error = self._guard(request)
        if guard_error:
            return guard_error

        day = parse_date(request.query_params.get('date'), timezone.localdate())
        try:
            return Response(
                {'day_book': day_book(business.id, day)},
                status=status.HTTP_200_OK
            )
        except Exception as error:
            print(error)
            return Response(
                {'detail': 'Internal Server Error.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(['GET'], detail=False, url_path='daily-summary', url_name='daily-summary')
    def daily_summary(self, request):
        business, guard_error = self._guard(request)
        if guard_error:
            return guard_error

        day = parse_date(request.query_params.get('date'), timezone.localdate())
        try:
            return Response(
                {'daily_summary': daily_summary(business.id, day)},
                status=status.HTTP_200_OK
            )
        except Exception as error:
            print(error)
            return Response(
                {'detail': 'Internal Server Error.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(['GET'], detail=False, url_path='profit-estimate', url_name='profit-estimate')
    def profit_estimate(self, request):
        business, guard_error = self._guard(request)
        if guard_error:
            return guard_error

        try:
            this_from, this_to = month_bounds()
            prev_from, prev_to = previous_month_bounds()
            return Response(
                {
                    'profit_estimate': {
                        'this_month': profit_estimate(
                            business.id, this_from, this_to),
                        'previous_month': profit_estimate(
                            business.id, prev_from, prev_to),
                        'is_estimate': True,
                    }
                },
                status=status.HTTP_200_OK
            )
        except Exception as error:
            print(error)
            return Response(
                {'detail': 'Internal Server Error.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(['GET'], detail=False, url_path='monthly-cash-trend', url_name='monthly-cash-trend')
    def monthly_cash_trend(self, request):
        business, guard_error = self._guard(request)
        if guard_error:
            return guard_error

        try:
            return Response(
                {
                    'monthly_cash_trend':
                        Transaction.objects.monthly_cash_trend(business.id)
                },
                status=status.HTTP_200_OK
            )
        except Exception as error:
            print(error)
            return Response(
                {'detail': 'Internal Server Error.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
