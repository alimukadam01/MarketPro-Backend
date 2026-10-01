from django.db.models.signals import post_save
from django.dispatch import receiver

from root.models import Business, Expense
from root.utils import local_date
from sales.models import PurchaseReceipt, SalesReceipt
from .models import MoneyAccount, Transaction


### every business gets a cash account, so the till always exists
@receiver(post_save, sender=Business)
def createDefaultMoneyAccount(sender, instance: Business, created, **kwargs):
    if not created:
        return
    # Also unswallowed. A business with no till cannot record a single
    # transaction, and every receipt mirror below fails on get_default_account.
    # Failing the signup outright beats handing someone a broken shop.
    MoneyAccount.objects.get_default_account(instance.id)


### mirror a sales payment into the money record
@receiver(post_save, sender=SalesReceipt)
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


# payment_status is now a derived property on both invoice models — no
# stored value to keep in sync, so no receivers are needed here.
