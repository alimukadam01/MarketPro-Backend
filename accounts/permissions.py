from rest_framework.permissions import BasePermission

from root.utils import get_active_business

from .utils import has_accounting_access


class HasAccountingAccess(BasePermission):
    """
    The module gate for every accounting endpoint.

    This answers 403 on reads as well as writes, unlike the targets gate, which
    hands back an empty list. The difference is deliberate: accounting data must
    not leak its existence, and a 403 is what the client needs in order to tell
    "you may not see this" apart from "the request failed". An empty list cannot
    carry that distinction, and a 404 from an empty queryset still leaks whether
    a row exists.

    Note it must be listed alongside IsAuthenticated on the viewset:
    permission_classes REPLACES DEFAULT_PERMISSION_CLASSES rather than adding to
    it, so naming this one alone would let anonymous callers through.

    Not 401. The client treats any 401 as a dead session and logs the user out
    (services/api.js interceptor -> AuthProvider), so a 401 here would sign
    someone out instead of telling them they lack access. The caller is
    authenticated; they are simply not permitted, which is what 403 means.
    """

    message = 'You do not have access to accounting.'

    def has_permission(self, request, view):
        business = get_active_business(request)
        if not business:
            # A missing business is a different failure with its own 400 from
            # the viewset. Let it through so the caller sees that message.
            return True

        return has_accounting_access(request, business)
