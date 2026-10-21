"""
The search types, registered by each app's `search.py`.

register() checks every field a type declares the moment it is registered.
The old engine let a mistyped field raise inside the search and swallowed
the error, which is how Products and Returned Items went unsearchable
without anyone noticing. A mistake here now stops the app from starting.
"""
from .types import SearchType

_REGISTRY = {}


class SearchRegistrationError(Exception):
    pass


def _check_path(model, path):
    # Building the lookup resolves every step of the path against the
    # models, without touching the database.
    try:
        model.objects.filter(**{f"{path}__isnull": True}).query.__str__()
    except Exception as error:
        raise SearchRegistrationError(
            f'{model.__name__}: "{path}" is not a field path ({error}).')


def register(search_type: SearchType):
    if search_type.key in _REGISTRY:
        raise SearchRegistrationError(
            f'A search type "{search_type.key}" is already registered.')
    if search_type.module is None and search_type.access is None and search_type.queryset is None:
        raise SearchRegistrationError(
            f'"{search_type.key}" has no module, access rule or queryset - '
            'nothing would decide who may see it.')

    model = search_type.model
    paths = (
        list(search_type.text_fields)
        + list(search_type.number_fields)
        + list(search_type.phone_fields)
        + list(search_type.choice_fields)
        + list(search_type.select_related)
    )
    if search_type.queryset is None:
        paths.append(search_type.business_field)
    for path in paths:
        _check_path(model, path)
    for path in search_type.order_by:
        _check_path(model, path.lstrip("-"))
    if search_type.breakdown and search_type.breakdown not in search_type.choice_fields:
        raise SearchRegistrationError(
            f'"{search_type.key}": breakdown "{search_type.breakdown}" must be one of its choice fields.')

    _REGISTRY[search_type.key] = search_type
    return search_type


def registered():
    """Every registered type, in the order the groups are shown."""
    return sorted(_REGISTRY.values(), key=lambda t: (t.priority, t.key))


def get(key):
    return _REGISTRY[key]
