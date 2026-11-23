"""
People, step 1 of 3: add the Person table and the new role columns.

Nothing existing changes here. Every new column is nullable or has a default,
so this step only adds. Step 2 (0028) fills them; step 3 (0029) makes them
required and removes the columns that moved to Person.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('root', '0026_product_fields'),
    ]

    operations = [
        migrations.CreateModel(
            name='Person',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=256)),
                ('phone', models.CharField(blank=True, max_length=256, null=True)),
                ('email', models.EmailField(blank=True, max_length=254, null=True)),
                ('address', models.TextField(blank=True, null=True)),
                ('business_name', models.CharField(blank=True, max_length=256, null=True)),
                ('notes', models.TextField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('business', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='people', to='root.business')),
                ('city', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.DO_NOTHING, to='root.city')),
            ],
        ),
        migrations.AddField(
            model_name='customer',
            name='person',
            field=models.OneToOneField(null=True, on_delete=django.db.models.deletion.CASCADE, related_name='customer', to='root.person'),
        ),
        migrations.AddField(
            model_name='customer',
            name='is_active',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='supplier',
            name='person',
            field=models.OneToOneField(null=True, on_delete=django.db.models.deletion.CASCADE, related_name='supplier', to='root.person'),
        ),
        migrations.AddField(
            model_name='supplier',
            name='is_active',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='supplier',
            name='created_at',
            field=models.DateTimeField(null=True),
        ),
    ]
