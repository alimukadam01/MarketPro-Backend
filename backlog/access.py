"""
Who may do what in the backlog.

  the owner           - everything: upload, manage, review, the glossary
  an employee         - what EmployeeAccess.permissions['backlog_entries']
                        grants (view / create = upload / edit / delete)
  data entry (role    - only the work endpoints, on items of their own
  'backlog')            business; everything else is closed to them at
                        authentication (audit/authentication.py)
"""
from rest_framework.permissions import BasePermission

from root.models import EmployeeAccess
from root.utils import get_active_business

MODULE = 'backlog_entries'
BACKLOG_ROLE = 'backlog'


def is_owner(user, business):
    return bool(business and business.owner_id == user.id)


def employee_can(user, business, action):
    access = EmployeeAccess.objects.filter(
        employee__user=user, employee__business=business).first()
    return bool(access and (access.permissions or {}).get(MODULE, {}).get(action))


def can(request, business, action):
    user = request.user
    if getattr(user, 'role', None) == BACKLOG_ROLE:
        return False
    return is_owner(user, business) or employee_can(user, business, action)


class BacklogModulePermission(BasePermission):
    """The owner's backlog pages. Reads need 'view', uploads 'create',
    changes 'edit', deletes 'delete'; reviewing and the glossary are the
    owner's alone (the views check that themselves)."""

    ACTIONS = {'GET': 'view', 'HEAD': 'view', 'OPTIONS': 'view',
               'POST': 'create', 'PUT': 'edit', 'PATCH': 'edit', 'DELETE': 'delete'}

    def has_permission(self, request, view):
        business = get_active_business(request)
        if not business:
            return False
        return can(request, business, self.ACTIONS.get(request.method, 'edit'))


class IsBusinessOwner(BasePermission):

    def has_permission(self, request, view):
        return is_owner(request.user, get_active_business(request))


class IsEntryWorker(BasePermission):
    """The entry screens: a data entry account, or the owner doing the
    work themselves."""

    def has_permission(self, request, view):
        business = get_active_business(request)
        if not business:
            return False
        return getattr(request.user, 'role', None) == BACKLOG_ROLE or is_owner(request.user, business)
