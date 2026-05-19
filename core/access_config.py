"""
UserAccessConfig
----------------
Given a User and a Business, computes the full access configuration
and returns it as a plain dict ready to be sent to the frontend.

Resolution rules (in order):
  1. If BusinessConfig says a module is OFF  → all flags false, regardless of role
  2. If user.role == 'admin'                 → all flags true for every enabled module
  3. If user.role == 'employee'              → read per-action flags from EmployeeAccess
"""

from root.models import EmployeeAccess


# All sidebar modules the system knows about
MODULES = [
    'sales',
    'purchases',
    'inventory',
    'products',
    'customers',
    'suppliers',
    'locations',
    'expenses',
    'projects',
    'quotations',
    'returned_items',
]

# Maps a module key → the BusinessConfig boolean field that gates it.
# None means the module is always enabled at the business level.
BUSINESS_CONFIG_MAP = {
    'sales':         'is_sales_enabled',
    'purchases':     'is_purchases_enabled',
    'inventory':     'is_inventory_enabled',
    'projects':      'is_projects_enabled',
    'quotations':    'is_quotations_enabled',
    'returned_items': 'is_returned_items_enabled',
    'products':      None,
    'customers':     None,
    'suppliers':     None,
    'locations':     None,
    'expenses':      None,
}

ACTIONS = ['view', 'create', 'edit', 'delete']

_ALL_TRUE  = {a: True  for a in ACTIONS}
_ALL_FALSE = {a: False for a in ACTIONS}


class UserAccessConfig:

    def __init__(self, user, business):
        self.user     = user
        self.business = business
        self.role     = user.role  # 'admin' | 'employee'

        # BusinessConfig lives on the business owner's user record
        try:
            self.biz_cfg = business.owner.business_config
        except Exception:
            self.biz_cfg = None

        # Only load EmployeeAccess for employees
        self.emp_perm = None
        if self.role == user.is_employee:
            self.emp_perm = EmployeeAccess.objects.filter(
                user=user, business=business
            ).first()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _module_enabled_in_business(self, module):
        """Check BusinessConfig flag; defaults to True if no config or no flag."""
        flag = BUSINESS_CONFIG_MAP.get(module)
        if flag is None or self.biz_cfg is None:
            return True
        return getattr(self.biz_cfg, flag, True)

    def _module_config(self, module):
        if not self._module_enabled_in_business(module):
            return {'enabled': False, **_ALL_FALSE}

        if self.role == self.user.ADMIN:
            return {'enabled': True, **_ALL_TRUE}

        # Employee path — all false if no EmployeeAccess row exists
        if not self.emp_perm:
            return {'enabled': False, **_ALL_FALSE}

        raw = self.emp_perm.permissions.get(module, {})
        return {
            'enabled': True,
            **{a: bool(raw.get(a, False)) for a in ACTIONS},
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def to_dict(self):
        return {
            'role':    self.role,
            'modules': {m: self._module_config(m) for m in MODULES},
        }
