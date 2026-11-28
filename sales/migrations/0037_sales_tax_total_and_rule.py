"""
Fix plan 5.1 and 5.3.

- tax_total: the tax each sales invoice charges, so profit can leave it out
  of revenue. Worked out once here for every existing invoice by the rule it
  was made under (tax on the price before the invoice's discount), from the
  figures it already stores, so no total changes.
- tax_after_discount: new invoices charge a percentage tax on the price
  after the discount. Every existing invoice is marked False and keeps the
  old rule, so no total a customer was given moves.
"""
from django.db import migrations, models


def fill_tax_total(apps, schema_editor):
    SalesInvoice = apps.get_model('sales', 'SalesInvoice')
    changed = []
    for invoice in SalesInvoice.objects.exclude(tax__isnull=True).only('id', 'tax', 'sub_total'):
        tax = invoice.tax if isinstance(invoice.tax, dict) else {}
        try:
            value = float(tax.get('value') or 0)
        except (TypeError, ValueError):
            continue
        if not value:
            continue
        if tax.get('type') == 'percentage':
            amount = float(invoice.sub_total or 0) * value / 100
        elif tax.get('type') == 'amount':
            amount = value
        else:
            continue
        invoice.tax_total = amount
        changed.append(invoice)
    SalesInvoice.objects.bulk_update(changed, ['tax_total'], batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0036_purchase_invoice_supplier_cascade'),
    ]

    operations = [
        # Existing rows take False (the old rule); new ones default to True.
        migrations.AddField(
            model_name='salesinvoice',
            name='tax_after_discount',
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name='salesinvoice',
            name='tax_after_discount',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='salesinvoice',
            name='tax_total',
            field=models.FloatField(default=0),
        ),
        migrations.RunPython(fill_tax_total, migrations.RunPython.noop),
    ]
