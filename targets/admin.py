from django.contrib import admin

from core.admin_masking import MaskedModelAdmin

from .models import ManualDataPoint, Target, TargetFilter

# Registered through MaskedModelAdmin so the figures render as asterisks.
# Target.target_value is a count for five of the nine data points, where masking
# is merely harmless; for the other four it is a revenue or purchase figure that
# gives away what the business is aiming for.
for model in (Target, TargetFilter, ManualDataPoint):
    admin.site.register(model, MaskedModelAdmin)
