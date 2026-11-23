"""
Opening balances move to the person, step 3 of 3: person, side and kind are
required (0011 filled and checked them), one opening per side of a person,
and the role columns go.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0011_opening_balance_person_backfill'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='partyopeningbalance',
            name='customer',
        ),
        migrations.RemoveField(
            model_name='partyopeningbalance',
            name='supplier',
        ),
        migrations.AlterField(
            model_name='partyopeningbalance',
            name='person',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='opening_balances', to='root.person'),
        ),
        migrations.AlterField(
            model_name='partyopeningbalance',
            name='side',
            field=models.CharField(choices=[('receivable', 'Receivable'), ('payable', 'Payable')], max_length=16),
        ),
        migrations.AlterField(
            model_name='partyopeningbalance',
            name='kind',
            field=models.CharField(choices=[('customer', 'Customer'), ('supplier', 'Supplier')], max_length=16),
        ),
        # The size only now; `side` carries the direction.
        migrations.AlterField(
            model_name='partyopeningbalance',
            name='amount',
            field=models.IntegerField(default=0),
        ),
        migrations.AddConstraint(
            model_name='partyopeningbalance',
            constraint=models.UniqueConstraint(fields=('person', 'side'), name='one_opening_balance_per_person_side'),
        ),
    ]
