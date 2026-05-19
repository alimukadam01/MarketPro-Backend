from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Employee, EmployeeAccess


def _crud(enabled):
    return {"view": enabled, "create": enabled, "edit": enabled, "delete": enabled}
