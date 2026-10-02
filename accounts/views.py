from django.db import transaction as db_transaction
from django.db.models import Q
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.filters import SearchFilter
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet, ModelViewSet

from root.models import Customer, Expense, Supplier
from root.utils import get_active_business, whatsapp_number
from sales.models import PurchaseReceipt, SalesReceipt
from .models import MoneyAccount, PartyOpeningBalance, Transaction
from .payments import (
    record_party_payment, resolve_party,
    settleable_items as party_settleable_items,
)
from .permissions import HasAccountingAccess
from .serializers import (
    MoneyAccountCreateSerializer, MoneyAccountSerializer,
    MoneyAccountUpdateSerializer, PartyOpeningBalanceSerializer,
    RecordPartyPaymentSerializer, SimpleMoneyAccountSerializer,
    SimpleTransactionSerializer, TransactionCreateSerializer,
    TransactionSerializer, TransactionUpdateSerializer,
)
from .utils import (
    daily_summary, day_book, has_accounting_access, month_bounds,
    opening_balance_remaining, parse_date, party_ledger, payables,
    previous_month_bounds, profit_estimate, receivables,
    uncleared_opening_balances,
)


NO_BUSINESS = {'detail': 'No active business exists. Please contact admin.'}
NO_ACCESS = {'detail': 'You do not have access to accounting.'}


class MoneyAccountViewSet(ModelViewSet):

    permission_classes = [IsAuthenticated, HasAccountingAccess]
    filter_backends = [SearchFilter, DjangoFilterBackend]
    filterset_fields = ['type', 'is_active']
    search_fields = ['id', 'name', 'type']

    def get_queryset(self):
        business = get_active_business(self.request)
        if not business:
            return MoneyAccount.objects.none()
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

    # HasAccountingAccess answers the module gate with a 403. It is listed
    # beside IsAuthenticated because permission_classes replaces the default
    # rather than adding to it.
    permission_classes = [IsAuthenticated, HasAccountingAccess]
    filter_backends = [SearchFilter, DjangoFilterBackend]
    filterset_fields = ['type', 'status', 'payment_method', 'account', 'date']
    search_fields = ['id', 'reference', 'notes', 'type', 'amount']

    def get_queryset(self):
        business = get_active_business(self.request)
        if not business:
            # none(), never []: DjangoFilterBackend reads queryset.model, and a
            # list has none, which turned every no-access retrieve into a 500.
            return Transaction.objects.none()
        # Explicitly ordered. Without an order_by the database is free to
        # return rows in any order it likes, which is what made the list jump
        # around. `date` is the day the money moved and is backdatable, so it
        # leads; `created_at` is a DateTimeField with microsecond precision, so
        # it breaks same-day ties by the order things were actually entered.
        return Transaction.objects.filter(
            business_id=business.id
        ).select_related('account', 'transfer_account').order_by(
            '-date', '-created_at', '-id')

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
            return {'request': self.request}
        return {
            # request is what ImageField needs to build an absolute URL; the
            # old context replaced DRF's default and dropped it, so every
            # image came back as a bare /media/... path.
            'request': self.request,
            'business_id': business.id,
            'user_id': self.request.user.id,
        }

    def create(self, request, *args, **kwargs):
        """
        Overridden for two reasons.

        The business guard moves here, so a missing business is a 400 instead
        of a KeyError inside the serializer, which used to surface as a 500.

        And the response is rendered with the READ serializer. The create
        serializer's field list is write-only, so DRF's stock create() answered
        201 with no id and no is_source_linked - the client could not tell a
        real write from the phantom 201 this module used to return, nor whether
        the saved row had become source-linked.
        """
        business = get_active_business(request)
        if not business:
            return Response(NO_BUSINESS, status=status.HTTP_400_BAD_REQUEST)

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        instance = serializer.save()

        return Response(
            TransactionSerializer(
                instance, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    @action(['POST'], detail=False, url_path='record-payment',
            url_name='record-payment')
    def record_payment(self, request):
        """
        One amount, several ticked items, one atomic write.

        Lives here rather than on CustomerViewSet because recording a payment
        is accounting: this viewset already carries HasAccountingAccess, while
        the party viewsets are gated on the customers/suppliers module, which
        is the wrong permission for moving money.

        Responds with the list of transactions actually written. A response
        body that is not the written rows is exactly how the phantom 201 used
        to hide.
        """
        business = get_active_business(request)
        if not business:
            return Response(NO_BUSINESS, status=status.HTTP_400_BAD_REQUEST)

        context = self.get_serializer_context()
        serializer = RecordPartyPaymentSerializer(
            data=request.data, context=context)
        serializer.is_valid(raise_exception=True)

        created = record_party_payment(
            business_id=business.id,
            user_id=request.user.id,
            party=serializer.validated_data['party'],
            party_id=serializer.validated_data['party_id'],
            amount=serializer.validated_data['amount'],
            item_ids=serializer.validated_data['items'],
            money_details=serializer.money_details(),
            context=context,
            order=serializer.validated_data['order'],
        )

        return Response(
            TransactionSerializer(created, many=True, context=context).data,
            status=status.HTTP_201_CREATED,
        )

    @staticmethod
    def _with_payment_group(transactions, business_id):
        """
        Widen a selection to every sibling in the same payment_group.

        A payment allocated across several items is ONE payment that happens
        to be stored as several transactions. Deleting or clearing one of them
        on its own would leave the rest as a half-recorded payment, which is
        not a state the books should be able to reach.
        """
        groups = {txn.payment_group
                  for txn in transactions if txn.payment_group}
        ids = [txn.id for txn in transactions]

        if not groups:
            return transactions

        return (
            Transaction.objects
            .filter(Q(id__in=ids) | Q(payment_group__in=groups),
                    business_id=business_id)
            .select_related('sales_receipt', 'purchase_receipt', 'expense')
        )

    @staticmethod
    def _delete_with_sources(transactions):
        """
        Deleting a source-backed transaction deletes the record that owns it.

        Deleting the receipt is the correct accounting outcome: an invoice's
        paid amount and payment status derive from its receipts, so removing
        one frees that share of the invoice again. The transaction goes with it
        through the FK's CASCADE.
        """
        for txn in transactions:
            source = (txn.sales_receipt
                      or txn.purchase_receipt
                      or txn.expense)
            if source is not None:
                source.delete()
            else:
                txn.delete()

    def destroy(self, request, *args, **kwargs):
        """
        This used to refuse with "delete the source payment or expense
        instead", which made sense when source-linked only meant "recorded on
        the invoice screen". A sale payment, purchase payment or expense can
        now be recorded here too, so refusing left the user unable to remove a
        row from the screen that created it.
        """
        try:
            instance = self.get_object()
            members = self._with_payment_group(
                [instance], instance.business_id)

            with db_transaction.atomic():
                self._delete_with_sources(members)

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
            # Grouped: one row per physical cheque, so an allocated payment
            # is cleared once rather than once per item it settled.
            cheques = Transaction.objects.pending_cheques_grouped(business.id)
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
        """
        Clearing or bouncing applies to the CHEQUE, not to one transaction.

        A cheque allocated across several items is stored as one transaction
        per item, all sharing a payment_group. The user clears the physical
        cheque, so every leg of it moves together - otherwise two of three
        invoices would show paid from a single cheque, which is not a real
        state.

        Nothing downstream needs unwinding, because no figure is stored:
        amount_paid skips receipts whose transaction is not 'C',
        on_account_totals filters status='C', and LIVE_RECEIPT excludes 'B'.
        So clearing settles every item at once and a bounce releases them all.
        """
        business = get_active_business(request)
        if not business:
            return Response(NO_BUSINESS, status=status.HTTP_400_BAD_REQUEST)
        if not has_accounting_access(request, business):
            return Response(NO_ACCESS, status=status.HTTP_403_FORBIDDEN)

        try:
            instance = self.get_object()

            # BUG-014: this used to write the status with no checks at all, so
            # a cash expense could be "bounced" and have its money silently
            # returned to the account, on a row that pending_cheques does not
            # even list. Only a cheque clears or bounces, and only a pending
            # cheque clears.
            if instance.payment_method != 'cheque':
                return Response(
                    {'detail': 'Only a cheque can be cleared or bounced.'},
                    status=status.HTTP_400_BAD_REQUEST)

            if instance.status == new_status:
                return Response(
                    {'detail': f'This cheque is already {new_status}.'},
                    status=status.HTTP_400_BAD_REQUEST)

            if new_status == 'C' and instance.status != 'PEN':
                return Response(
                    {'detail': 'Only a pending cheque can be cleared.'},
                    status=status.HTTP_400_BAD_REQUEST)

            with db_transaction.atomic():
                if instance.payment_group:
                    updated = Transaction.objects.filter(
                        payment_group=instance.payment_group,
                        business_id=business.id,
                    ).update(status=new_status)
                else:
                    instance.status = new_status
                    instance.save(update_fields=['status'])
                    updated = 1

            return Response(
                {'detail': 'OK', 'status': new_status, 'updated': updated},
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
            # This used to filter source-linked rows OUT and still answer
            # "Success", so selecting a sale payment or an expense deleted
            # nothing while the UI reported it had worked. Each one now takes
            # its owning record with it, exactly as destroy() does.
            selected = Transaction.objects.filter(
                id__in=transaction_ids, business_id=business.id
            ).select_related('sales_receipt', 'purchase_receipt', 'expense')

            # Selecting one leg of an allocated payment removes all of it.
            transactions = self._with_payment_group(selected, business.id)

            plain, receipts, purchases, expenses = [], [], [], []
            for txn in transactions:
                if txn.sales_receipt_id:
                    receipts.append(txn.sales_receipt_id)
                elif txn.purchase_receipt_id:
                    purchases.append(txn.purchase_receipt_id)
                elif txn.expense_id:
                    expenses.append(txn.expense_id)
                else:
                    plain.append(txn.id)

            deleted = len(plain) + len(receipts) + len(purchases) + len(expenses)

            with db_transaction.atomic():
                # Deleting a source cascades to its transaction.
                SalesReceipt.objects.filter(id__in=receipts).delete()
                PurchaseReceipt.objects.filter(id__in=purchases).delete()
                Expense.objects.filter(id__in=expenses).delete()
                Transaction.objects.filter(id__in=plain).delete()

            # The count is what lets the caller tell a real delete from a
            # selection that matched nothing.
            return Response({'detail': 'Success.', 'deleted': deleted},
                            status=status.HTTP_200_OK)
        except Exception as error:
            print(error)
            return Response(
                {'detail': 'Internal Server Error.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class PartyOpeningBalanceViewSet(ModelViewSet):

    permission_classes = [IsAuthenticated, HasAccountingAccess]
    serializer_class = PartyOpeningBalanceSerializer
    filter_backends = [SearchFilter, DjangoFilterBackend]
    filterset_fields = ['customer', 'supplier']
    search_fields = ['id', 'amount']

    def get_queryset(self):
        business = get_active_business(self.request)
        if not business:
            return PartyOpeningBalance.objects.none()
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

    @action(['GET'], detail=False, url_path='opening-balances',
            url_name='opening-balances')
    def opening_balances(self, request):
        """
        Parties still carrying an unsettled opening balance, with what is left
        on it. ?party=supplier (default) or ?party=customer.

        Separate from payables/receivables on purpose: those are the whole
        relationship including invoices, while an on-account payment settles
        only the balance brought across from the paper khaata.
        """
        business, guard_error = self._guard(request)
        if guard_error:
            return guard_error

        party = request.query_params.get('party', 'supplier')
        if party not in ('customer', 'supplier'):
            return Response({'detail': 'Bad Request.'},
                            status=status.HTTP_400_BAD_REQUEST)

        try:
            return Response(
                {'opening_balances': uncleared_opening_balances(
                    business.id, party)},
                status=status.HTTP_200_OK
            )
        except Exception as error:
            print(error)
            return Response(
                {'detail': 'Internal Server Error.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(['GET'], detail=False, url_path='settleable-items',
            url_name='settleable-items')
    def settleable_items(self, request):
        """
        Everything one party's money could settle, oldest first:
        their uncleared opening balance, then every invoice with room left.
        ?customer_id= or ?supplier_id=.

        `room` here is authoritative - it comes from the same invoice_room()
        the write path validates against, so the dialog can never offer more
        than the server will accept. The client must not compute it: the
        frontend's own `invoicePending` subtracts amount_paid, which excludes
        pending cheques, and would offer money the server then refuses.
        """
        business, guard_error = self._guard(request)
        if guard_error:
            return guard_error

        party, party_id = resolve_party(
            business.id,
            customer_id=request.query_params.get('customer_id'),
            supplier_id=request.query_params.get('supplier_id'),
        )
        if not party:
            return Response({'detail': 'Not Found'},
                            status=status.HTTP_404_NOT_FOUND)

        try:
            opening, settled, remaining = opening_balance_remaining(
                business.id, party, party_id)

            # as_of_date rides along: the party page needs the STORED date, not
            # today. Without it the form fell back to today's date, so the card
            # misreported when the balance was taken and re-saving silently
            # moved it forward.
            row = PartyOpeningBalance.objects.filter(
                business_id=business.id, **{f'{party}_id': party_id}).first()

            return Response({
                'items': party_settleable_items(business.id, party, party_id),
                'opening_balance': {
                    'id': row.id if row else None,
                    'amount': opening,
                    'settled': settled,
                    'remaining': remaining,
                    'as_of_date': row.as_of_date.isoformat() if row else None,
                },
            }, status=status.HTTP_200_OK)
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
