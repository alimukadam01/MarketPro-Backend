from django.contrib import admin

from .models import (
    MoneyAccount, PartyOpeningBalance, PartyPayment, Transaction
)


@admin.register(MoneyAccount)
class MoneyAccountAdmin(admin.ModelAdmin):
    list_display = ('name', 'type', 'business', 'is_default', 'is_active')
    list_filter = ('type', 'is_active', 'is_default')
    search_fields = ('name',)


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ('id', 'date', 'type', 'amount', 'account',
                    'payment_method', 'status')
    list_filter = ('type', 'status', 'payment_method')
    search_fields = ('id', 'reference', 'notes')


@admin.register(PartyPayment)
class PartyPaymentAdmin(admin.ModelAdmin):
    list_display = ('id', 'customer', 'supplier', 'transaction')


@admin.register(PartyOpeningBalance)
class PartyOpeningBalanceAdmin(admin.ModelAdmin):
    list_display = ('id', 'customer', 'supplier', 'amount', 'as_of_date')
