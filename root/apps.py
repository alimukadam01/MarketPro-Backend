from django.apps import AppConfig


class RootConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'root'

    def ready(self):
        import root.signals

        # Global search: every app declares its search types in its own
        # search.py; importing them registers them (root/search/__init__.py).
        # Root's own are in root/search_types.py, since root.search is the
        # search package itself.
        from django.utils.module_loading import autodiscover_modules
        autodiscover_modules("search")
        import root.search_types  # noqa: F401
