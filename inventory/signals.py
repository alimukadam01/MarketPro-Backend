from django.db.models.signals import post_save
from django.dispatch import receiver
from root.models import Business
from .models import Inventory
from audit.context import automatic

@receiver(post_save, sender=Business)
@automatic('signal:create_inventory_for_new_business')
def create_inventory_for_new_business(sender, **kwargs):
    if kwargs['created']:
        Inventory.objects.create(
            business_id = kwargs['instance'].id
        )

    return

