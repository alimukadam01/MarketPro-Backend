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
    try:
        MoneyAccount.objects.get_default_account(instance.id)
    except Exception as error:
        print(error)


### mirror a sales payment into the money record
@receiver(post_save, sender=SalesReceipt)
def createTransactionForSalesReceipt(sender, instance: SalesReceipt, **kwargs):
    try:
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
    except Exception as error:
        print(error)


### mirror a purchase payment into the money record
@receiver(post_save, sender=PurchaseReceipt)
def createTransactionForPurchaseReceipt(sender, instance: PurchaseReceipt, **kwargs):
    try:
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
    except Exception as error:
        print(error)


### an expense is money going out, so it is a transaction too
@receiver(post_save, sender=Expense)
def createTransactionForExpense(sender, instance: Expense, **kwargs):
    try:
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
    except Exception as error:
        print(error)


# payment_status is now a derived property on both invoice models — no
# stored value to keep in sync, so no receivers are needed here.
