from django.contrib import admin

from core.admin_masking import MaskedModelAdmin

from .models import BacklogEntry

# Registered through MaskedModelAdmin for consistency; BacklogEntry holds no
# currency amount of its own.
admin.site.register(BacklogEntry, MaskedModelAdmin)
