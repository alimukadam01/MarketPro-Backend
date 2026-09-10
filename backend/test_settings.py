"""Settings for `manage.py test`.

    python manage.py test --settings=backend.test_settings

The test schema is built straight from the models rather than by replaying
migrations, because sales/migrations/0010_remove_purchaseinvoice_... removes
five constraints that no earlier migration adds to the migration state:

    ValueError: No constraint named
    is_restocked_and_is_partially_restocked_mutually_exclusive_pi
    on model PurchaseInvoice

Databases that already existed when 0010 ran migrated fine -- the constraints
were present in the real schema -- but a *fresh* database cannot be built from
the migration history, which is why `manage.py test` has never run in this
project. Disabling migrations sidesteps the broken history and tests the models
as they are defined today.

Repairing 0010 is a separate change: it edits migration history on a project
with a live production database, so it wants its own review.
"""

from .settings import *  # noqa: F401,F403


class _SchemaFromModels:
    """Report every app as having no migrations.

    django.test.utils.setup_databases then asks the schema editor to create
    tables directly from the current models.
    """

    def __contains__(self, app_label):
        return True

    def __getitem__(self, app_label):
        return None


MIGRATION_MODULES = _SchemaFromModels()
