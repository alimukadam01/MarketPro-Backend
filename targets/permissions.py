from rest_framework.permissions import SAFE_METHODS, BasePermission

from root.utils import get_active_business

from .utils import has_targets_access


class HasTargetsWriteAccess(BasePermission):
    """
    The add-on gate for anything that changes data.

    Gating get_queryset() alone is not enough, and it is worth being explicit
    about why: ModelViewSet.create() never touches get_queryset, so a business
    without the add-on could POST a target it was not allowed to read back. The
    same holds for update and destroy on a pk the caller already knows, and for
    the bulk-delete and duplicate actions.

    Reads are deliberately left to get_queryset(), which returns an empty
    queryset. A denied list has to come back as 200 with [] rather than 403,
    because that is what every other module does and what the client expects:
    APIPackage.list() turns any non-200 into null, which the pages read as "the
    request failed" and report as an error toast rather than as no access. The
    KPI endpoints are the exception and answer 403 through their own _guard,
    since there is no sensible empty figure to return.
    """

    message = 'You do not have access to targets.'

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True

        business = get_active_business(request)
        if not business:
            # A missing business is a different failure, and the viewsets report
            # it with the project's standard 400. Let it through so the caller
            # sees that message instead of this one.
            return True

        return has_targets_access(request, business)
