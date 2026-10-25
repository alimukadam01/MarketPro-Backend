"""
JWT authentication that also says who is acting, and keeps data entry
accounts inside the backlog.

DRF authenticates inside the view, so this - not middleware - is the first
place the signed-in user is known. It is also the one class every API view
runs: no view overrides authentication_classes, while several replace
permission_classes (accounts, targets), which a global permission class
would silently lose. So the data-entry lockout lives here.
"""
from rest_framework.exceptions import PermissionDenied
from rest_framework_simplejwt.authentication import JWTAuthentication

from . import context

BACKLOG_ROLE = 'backlog'


def backlog_role_allowed(view, request):
    """
    Whether a data entry account may use this view.

    A view opts in with `allow_backlog_role`: True for the whole view, or a
    set naming what is allowed - an action ('me'), an action and method
    ('me:GET'), or, on a plain APIView, a method ('GET').
    """
    allowed = getattr(view, 'allow_backlog_role', False)
    if allowed is True:
        return True
    if not allowed:
        return False
    action = getattr(view, 'action', None)
    method = request.method.upper()
    return bool({f'{action}:{method}', action, method} & set(allowed))


class AuditedJWTAuthentication(JWTAuthentication):

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            return None
        user, _token = result
        context.set_actor(user)

        if getattr(user, 'role', None) == BACKLOG_ROLE:
            view = (getattr(request, 'parser_context', None) or {}).get('view')
            if view is None or not backlog_role_allowed(view, request):
                raise PermissionDenied('This account is for data entry only.')
        return result
