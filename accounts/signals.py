from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from root.models import Business, Expense
from root.utils import local_date
from sales.models import PurchaseReceipt, SalesReceipt
from sales.utils import deleting_with_invoice, sync_charge_expenses
from .models import MoneyAccount, Transaction
from audit.context import automatic


### every business gets a cash account, so the till always exists
@receiver(post_save, sender=Business)
@automatic('signal:create_default_money_account')
def createDefaultMoneyAccount(sender, instance: Business, created, **kwargs):
    if not created:
        return
    # Also unswallowed. A business with no till cannot record a single
    # transaction, and every receipt mirror below fails on get_default_account.
    # Failing the signup outright beats handing someone a broken shop.
    MoneyAccount.objects.get_default_account(instance.id)


### mirror a sales payment into the money record
@receiver(post_save, sender=SalesReceipt)
@automatic('signal:create_transaction_for_sales_receipt')
def createTransactionForSalesReceipt(sender, instance: SalesReceipt, **kwargs):
    # No try/except. A receipt whose transaction never appears is a receipt that
    # paid down an invoice without the money leaving any account - the books
    # silently disagree with themselves. Swallowing that hid it; letting it
    # raise rolls the receipt back with it.
    business_id = instance.sales_invoice.business_id
    account = MoneyAccount.objects.get_default_account(business_id)

    transaction, created = Transaction.objects.get_or_create(
        sales_receipt=instance,
        defaults={
            'business_id': business_id,
            'account': account,
            'type': 'sale_payment',
            'amount': round(instance.amount or 0),
            'date': local_date(instance.created_at),
            'notes': instance.desc,
        },
    )

    if not created:
        transaction.amount = round(instance.amount or 0)
        transaction.save(update_fields=['amount'])


### mirror a purchase payment into the money record
@receiver(post_save, sender=PurchaseReceipt)
@automatic('signal:create_transaction_for_purchase_receipt')
def createTransactionForPurchaseReceipt(sender, instance: PurchaseReceipt, **kwargs):
    # See the sales mirror above for why this no longer swallows.
    business_id = instance.purchase_invoice.business_id
    account = MoneyAccount.objects.get_default_account(business_id)

    transaction, created = Transaction.objects.get_or_create(
        purchase_receipt=instance,
        defaults={
            'business_id': business_id,
            'account': account,
            'type': 'purchase_payment',
            'amount': round(instance.amount or 0),
            'date': local_date(instance.created_at),
            'notes': instance.desc,
        },
    )

    if not created:
        transaction.amount = round(instance.amount or 0)
        transaction.save(update_fields=['amount'])


### an expense is money going out, so it is a transaction too
@receiver(post_save, sender=Expense)
@automatic('signal:create_transaction_for_expense')
def createTransactionForExpense(sender, instance: Expense, **kwargs):
    # See the sales mirror above for why this no longer swallows.
    account = MoneyAccount.objects.get_default_account(instance.business_id)

    transaction, created = Transaction.objects.get_or_create(
        expense=instance,
        defaults={
            'business_id': instance.business_id,
            'account': account,
            'type': 'expense',
            'amount': round(instance.amount or 0),
            'date': local_date(instance.created_at),
            'notes': instance.desc,
        },
    )

    if not created:
        transaction.amount = round(instance.amount or 0)
        transaction.save(update_fields=['amount'])


### a sales payment decides which account the invoice's extra charges leave
@receiver(post_save, sender=Transaction)
@automatic('signal:sync_charges_after_sale_payment_saved')
def syncChargesAfterSalePaymentSaved(sender, instance: Transaction, **kwargs):
    # Covers a payment arriving (the mirror above creates its transaction, and
    # apply_money_details then sets the account), and a single cheque clearing
    # or bouncing. A grouped cheque changes status through a queryset update,
    # which sends no signal; the mark-cleared / mark-bounced action syncs
    # those itself.
    #
    # The expense transactions sync_charge_expenses makes carry no
    # sales_receipt, so they return here at once and nothing loops.
    if not instance.sales_receipt_id:
        return
    sync_charge_expenses(instance.sales_receipt.sales_invoice)


@receiver(post_delete, sender=SalesReceipt)
@automatic('signal:sync_charges_after_sale_payment_deleted')
def syncChargesAfterSalePaymentDeleted(sender, instance: SalesReceipt, **kwargs):
    # The payment that decided the account may be the one going. Not when the
    # whole invoice is being deleted: see deleting_with_invoice.
    if deleting_with_invoice(kwargs.get('origin')):
        return
    sync_charge_expenses(instance.sales_invoice)


# payment_status is now a derived property on both invoice models — no
# stored value to keep in sync, so no receivers are needed here.
