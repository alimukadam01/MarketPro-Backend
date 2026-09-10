from django.contrib import admin

from core.admin_masking import MaskedAmountsAdminMixin

from .models import (
    MoneyAccount, PartyOpeningBalance, PartyPayment, Transaction
)


@admin.register(MoneyAccount)
class MoneyAccountAdmin(MaskedAmountsAdminMixin, admin.ModelAdmin):
    list_display = ('name', 'type', 'business', 'is_default', 'is_active')
    list_filter = ('type', 'is_active', 'is_default')
    search_fields = ('name',)


@admin.register(Transaction)
class TransactionAdmin(MaskedAmountsAdminMixin, admin.ModelAdmin):
    # 'amount' stays listed: get_list_display swaps it for a masked, unsortable
    # column. Do not add an ordering on it -- see rule 3 in core/admin_masking.
    list_display = ('id', 'date', 'type', 'amount', 'account',
                    'payment_method', 'status')
    list_filter = ('type', 'status', 'payment_method')
    search_fields = ('id', 'reference', 'notes')


@admin.register(PartyPayment)
class PartyPaymentAdmin(MaskedAmountsAdminMixin, admin.ModelAdmin):
    # 'transaction' renders the related object, which the template stringifies
    # via Transaction.__str__ -- printing the amount on the changelist. The FK
    # branch of get_list_display masks it.
    list_display = ('id', 'customer', 'supplier', 'transaction')


@admin.register(PartyOpeningBalance)
class PartyOpeningBalanceAdmin(MaskedAmountsAdminMixin, admin.ModelAdmin):
    list_display = ('id', 'customer', 'supplier', 'amount', 'as_of_date')
