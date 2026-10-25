from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'core'

    def ready(self):
        # A data entry account may read its own user record and nothing else
        # of djoser's (audit/authentication.py) - not change or delete it.
        from djoser.views import UserViewSet
        UserViewSet.allow_backlog_role = {'me:GET'}
