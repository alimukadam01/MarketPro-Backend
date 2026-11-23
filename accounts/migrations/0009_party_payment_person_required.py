"""
People, Phase 4, step 3 of 3: PartyPayment.person is required (0008 filled
and checked it). Kept apart from 0008 so the data step and the schema change
each run as their own unit on PostgreSQL.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0008_party_payment_person_backfill'),
    ]

    operations = [
        migrations.AlterField(
            model_name='partypayment',
            name='person',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='party_payments', to='root.person'),
        ),
    ]
