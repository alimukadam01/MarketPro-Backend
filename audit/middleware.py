from . import context


class AuditContextMiddleware:
    """A clean audit context for each request, so one request's user can
    never leak into the next one served by the same thread."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        context.start_request()
        try:
            return self.get_response(request)
        finally:
            context.end_request()

    def process_view(self, request, view_func, view_args, view_kwargs):
        """The user a session signed in - the Django admin's. The API's JWT
        user is set later, by AuditedJWTAuthentication, inside the view; by
        here AuthenticationMiddleware has run, wherever this sits."""
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated:
            context.set_actor(user)
