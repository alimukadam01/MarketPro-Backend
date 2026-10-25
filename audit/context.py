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
"""
import functools
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

_actor = ContextVar('audit_actor', default=None)
_source = ContextVar('audit_source', default='system')
_request_id = ContextVar('audit_request_id', default='')


def actor():
    return _actor.get()


def source():
    return _source.get()


def request_id():
    return _request_id.get()


def start_request():
    """A fresh context for a request: nobody yet, source 'ui'."""
    _actor.set(None)
    _source.set('ui')
    _request_id.set(uuid.uuid4().hex)


def end_request():
    _actor.set(None)
    _source.set('system')
    _request_id.set('')


def set_actor(user):
    _actor.set(user if getattr(user, 'is_authenticated', False) else None)


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
