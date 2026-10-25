from django.apps import AppConfig, apps

# The project's own apps. Their models are audited; Django's and the
# libraries' are not.
AUDITED_APPS = ('core', 'root', 'inventory', 'sales', 'projects', 'backlog', 'accounts', 'targets')


class AuditConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'audit'

    def ready(self):
        from .signals import connect_all
        connect_all([
            model for model in apps.get_models()
            if model._meta.app_label in AUDITED_APPS
        ])
