from django.conf import settings
from django.db import models


class AuditEvent(models.Model):
    """
    One write to one row: who, how, what changed.

    Written for every save and delete of a project model (audit/signals.py)
    and for the queryset and bulk writes that send no signals
    (audit/helpers.py). Never edited.
    """

    ACTIONS = [
        ('create', 'Create'),
        ('update', 'Update'),
        ('delete', 'Delete'),
        ('bulk_create', 'Bulk create'),
        ('bulk_update', 'Bulk update'),
    ]

    business = models.ForeignKey(
        'root.Business', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='audit_events')
    # Null for work nobody signed in did: a command, an import, a job.
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='audit_events')
    # 'ui', 'system', 'import' or 'signal:<name>' (audit/context.py).
    source = models.CharField(max_length=64)
    model = models.CharField(max_length=128)
    object_id = models.CharField(max_length=64)
    action = models.CharField(max_length=16, choices=ACTIONS)
    # {field: [before, after]}; a create has None befores, a delete None afters.
    changes = models.JSONField(default=dict)
    request_id = models.CharField(max_length=32, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['business', 'created_at']),
            models.Index(fields=['model', 'object_id']),
        ]
        ordering = ['-created_at', '-id']

    def __str__(self):
        return f'{self.action} {self.model}#{self.object_id} by {self.actor_id or "system"}'
