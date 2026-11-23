"""
Opening balances move to the person, step 1 of 3: the new columns, empty.

Each opening balance belongs to the person, one per side (Receivable,
Payable), whatever roles they hold, and records the khaata it counts on
(kind). 0011 fills them; 0012 makes them required and drops the role columns.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0009_party_payment_person_required'),
        ('root', '0030_people_permission'),
    ]

    operations = [
        migrations.AddField(
            model_name='partyopeningbalance',
            name='person',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, related_name='opening_balances', to='root.person'),
        ),
        migrations.AddField(
            model_name='partyopeningbalance',
            name='side',
            field=models.CharField(choices=[('receivable', 'Receivable'), ('payable', 'Payable')], max_length=16, null=True),
        ),
        migrations.AddField(
            model_name='partyopeningbalance',
            name='kind',
            field=models.CharField(choices=[('customer', 'Customer'), ('supplier', 'Supplier')], max_length=16, null=True),
        ),
    ]
