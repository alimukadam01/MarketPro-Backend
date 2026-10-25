from django.contrib import admin

from core.admin_masking import MaskedModelAdmin

from .models import (
    BacklogEntry, BacklogNewRecord, BacklogOutput, BacklogReview, Glossary, GlossaryEntry, GlossarySuggestion,
    UploadBatch,
)

# Registered through MaskedModelAdmin for consistency; none of these holds a
# currency amount of its own.
for model in (BacklogEntry, BacklogOutput, BacklogNewRecord, BacklogReview, UploadBatch,
              Glossary, GlossaryEntry, GlossarySuggestion):
    admin.site.register(model, MaskedModelAdmin)
