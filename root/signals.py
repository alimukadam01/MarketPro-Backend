from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Business, Customer, Employee, EmployeeAccess


def _crud(enabled):
    return {"view": enabled, "create": enabled, "edit": enabled, "delete": enabled}


### every business gets a counter-sale customer, so a walk-in invoice can be
### raised before the buyer is known
@receiver(post_save, sender=Business)
def createWalkInCustomer(sender, instance: Business, created, **kwargs):
    if not created:
        return
    try:
        # A savepoint, so a failure here cannot poison the transaction the
        # business was created in. Swallowing the error is only safe if the
        # statement is rolled back with it — on Postgres an aborted statement
        # without one takes the whole transaction down regardless.
        with transaction.atomic():
            Customer.objects.create_walk_in(instance.id)
    except Exception as error:
        print(error)
