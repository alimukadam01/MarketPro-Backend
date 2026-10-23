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
