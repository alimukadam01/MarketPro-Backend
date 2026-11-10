"""
Who is acting, and how, for the audit trail.

One context per request (reset by AuditContextMiddleware) or per shell or
command run, where nothing sets it and the defaults - no actor, source
'system' - stand.

  actor   - the signed-in user. Set by AuditedJWTAuthentication, since DRF
            authenticates inside the view where middleware never sees the
            JWT user.
  source  - 'ui' for a request, 'system' without one, and 'signal:<name>' /
            'import' inside audit_source() blocks: the automatic work a UI
            action set off still records the user who set it off, with the
            source saying it was automatic.
  business - the business the actor is working in, for an event whose record
            leads to none (recorder.record). Worked out only when an event
            needs it, then kept for the rest of the request.
"""
import functools
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

_actor = ContextVar('audit_actor', default=None)
_source = ContextVar('audit_source', default='system')
_request_id = ContextVar('audit_request_id', default='')
# A callable giving the actor's business, and its answer once asked: a
# request that writes only records with a business never pays for the lookup.
_business_lookup = ContextVar('audit_business_lookup', default=None)
_business = ContextVar('audit_business', default=None)

_UNKNOWN = object()


def actor():
    return _actor.get()


def source():
    return _source.get()


def request_id():
    return _request_id.get()


def business_id():
    """The id of the business the actor is working in, or None."""
    cached = _business.get()
    if cached is not None:
        return None if cached is _UNKNOWN else cached
    lookup = _business_lookup.get()
    if lookup is None:
        return None
    business = lookup()
    found = getattr(business, 'pk', None)
    _business.set(found if found is not None else _UNKNOWN)
    return found


def start_request():
    """A fresh context for a request: nobody yet, source 'ui'."""
    _actor.set(None)
    _source.set('ui')
    _request_id.set(uuid.uuid4().hex)
    _business_lookup.set(None)
    _business.set(None)


def end_request():
    _actor.set(None)
    _source.set('system')
    _request_id.set('')
    _business_lookup.set(None)
    _business.set(None)


def set_actor(user):
    """The signed-in user, and with them how to find their business."""
    if not getattr(user, 'is_authenticated', False):
        _actor.set(None)
        return
    _actor.set(user)
    _business.set(None)
    _business_lookup.set(lambda: user_business(user))


def user_business(user):
    """The business a user works in: the one they own and have active, else
    the one that employs them (root.utils.get_active_business's rule,
    without its exception for a user with neither)."""
    from root.models import Business, Employee
    owned = Business.objects.filter(owner_id=user.pk, is_active=True).first()
    if owned:
        return owned
    employment = Employee.objects.filter(user_id=user.pk).select_related('business').first()
    return employment.business if employment else None


@contextmanager
def audit_source(name):
    """Mark the writes inside as automatic: audit_source('signal:restock')."""
    token = _source.set(name)
    try:
        yield
    finally:
        _source.reset(token)


def automatic(name):
    """audit_source as a decorator, for signal receivers and the helpers
    that move stock or re-derive a status:

        @receiver(post_save, sender=Expense)
        @automatic('signal:expense_transaction')
        def createTransactionForExpense(...): ...

    An outer automatic block keeps its name, so a helper called from a
    signal is recorded under the signal that set it off.
    """
    def decorate(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            if _source.get().startswith('signal:'):
                return func(*args, **kwargs)
            with audit_source(name):
                return func(*args, **kwargs)
        return wrapper
    return decorate
