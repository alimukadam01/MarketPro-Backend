"""
Who may see which search types.

The rule is the one the app already applies to every module
(core/serializers.get_permissions):

  admin     - allowed, unless the business config switches the module off
              (BusinessConfig.<module>; a module with no flag is allowed)
  employee  - EmployeeAccess.permissions[module]["view"]

Accounting and targets keep their own checks, passed to a type as `access`.
"""


class Access:
    """One user's view permissions in one business, read once per search."""

    def __init__(self, request, business):
        self.request = request
        self.business = business
        self.user = request.user
        self.is_admin = getattr(self.user, "role", None) == "admin"
        self._config = None
        self._permissions = None

    def _business_config(self):
        if self._config is None:
            try:
                self._config = self.business.config
            except Exception:
                self._config = False
        return self._config or None

    def _employee_permissions(self):
        if self._permissions is None:
            self._permissions = {}
            employee = (self.user.emp_records
                        .filter(business_id=self.business.id)
                        .select_related("access")
                        .first())
            try:
                self._permissions = employee.access.permissions or {}
            except Exception:
                self._permissions = {}
        return self._permissions

    def can_view_module(self, module):
        if not self.user or not self.user.is_authenticated:
            return False
        if self.is_admin:
            config = self._business_config()
            return bool(getattr(config, module, True)) if config else True
        return bool(self._employee_permissions().get(module, {}).get("view"))

    def can_view(self, search_type):
        if search_type.access is not None:
            return bool(search_type.access(self.request, self.business, self))
        if search_type.module is None:
            # Visibility is the type's own queryset (backlog entries).
            return True
        return self.can_view_module(search_type.module)


def admin_only(request, business, access):
    return access.is_admin
