"""
People, step 3 of 3: every role now has a person (0028 checked it), so the
link becomes required and the details that moved to Person leave the roles,
along with Customer.total_sales, which nothing ever wrote (register S10, R8).

Kept apart from 0028 so the data step and these schema changes each run as
their own unit on PostgreSQL.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('root', '0028_person_backfill'),
    ]

    operations = [
        migrations.AlterField(
            model_name='customer',
            name='person',
            field=models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='customer', to='root.person'),
        ),
        migrations.AlterField(
            model_name='supplier',
            name='person',
            field=models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='supplier', to='root.person'),
        ),
        migrations.AlterField(
            model_name='supplier',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True),
        ),
        migrations.RemoveField(model_name='customer', name='name'),
        migrations.RemoveField(model_name='customer', name='phone'),
        migrations.RemoveField(model_name='customer', name='email'),
        migrations.RemoveField(model_name='customer', name='address'),
        migrations.RemoveField(model_name='customer', name='city'),
        migrations.RemoveField(model_name='customer', name='notes'),
        migrations.RemoveField(model_name='customer', name='total_sales'),
        migrations.RemoveField(model_name='supplier', name='name'),
        migrations.RemoveField(model_name='supplier', name='business_name'),
        migrations.RemoveField(model_name='supplier', name='phone'),
        migrations.RemoveField(model_name='supplier', name='email'),
        migrations.RemoveField(model_name='supplier', name='notes'),
    ]
