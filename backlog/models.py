from django.db import models
from django.conf import settings
from root.models import Business, Employee, BaseQuerySet


class BacklogEntryQuerySet(BaseQuerySet):
    pass


class BacklogEntryManager(models.Manager):

    def get_queryset(self):
        return BacklogEntryQuerySet(self.model)

    def for_employee(self, business_id, user_id):
        return (
            self.get_queryset()
            .for_business(business_id)
            .filter(assigned_to__user_id=user_id, is_done=False)
        )


class BacklogEntry(models.Model):

    TYPE_CHOICES = [
        ('sales_invoice', 'Sales Invoice'),
        ('purchase_invoice', 'Purchase Invoice'),
    ]

    business = models.ForeignKey(
        Business, on_delete=models.CASCADE, related_name='backlog_entries'
    )
    image = models.ImageField(upload_to='backlog')
    type = models.CharField(max_length=256, choices=TYPE_CHOICES)
    assigned_to = models.ForeignKey(
        Employee, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='backlog_entries'
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='created_backlogs'
    )
    notes = models.TextField(null=True, blank=True)
    is_done = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = BacklogEntryManager()

    def __str__(self):
        return f"{self.id}-{self.type}"
