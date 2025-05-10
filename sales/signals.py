from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import SalesInvoice, SalesInvoiceItem


### add update functionality
@receiver(post_save, sender=SalesInvoiceItem)
def update_totals_for_new_si_item(sender, **kwargs):
    if kwargs['created']:
        instance = kwargs['instance']
        sales_invoice = SalesInvoice.objects.get(id = instance.sales_invoice.id)

        sales_invoice.sub_total += instance.unit_price * instance.quantity
        sales_invoice.total += instance.unit_price * instance.quantity

        sales_invoice.save()
        return
    

### Create delete signal

