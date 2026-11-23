"""
People, Phase 4, step 1 of 3: loans with a person, and set-offs.

New transaction types: loan_given and loan_recovered (lending money out and
getting it back), and set_off_receivable / set_off_payable (one person's two
sides settled against each other, moving no money). PartyPayment gains the
person it is about, and, on a loan repayment, the loan it repays.

Adds only: `person` is nullable here, filled in by 0008 and made required by
0009.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0006_party_payment_refund_invoice'),
        ('root', '0030_people_permission'),
    ]

    operations = [
        migrations.AlterField(
            model_name='transaction',
            name='type',
            field=models.CharField(choices=[('sale_payment', 'Sale Payment'), ('customer_receipt', 'Customer Payment'), ('purchase_return_refund', 'Purchase Return Refund'), ('owner_capital', 'Owner Capital'), ('loan_received', 'Loan Received'), ('other_income', 'Other Income'), ('purchase_payment', 'Purchase Payment'), ('supplier_payment', 'Supplier Payment'), ('sales_return_refund', 'Sales Return Refund'), ('expense', 'Expense'), ('salary_payment', 'Salary Payment'), ('owner_drawings', 'Owner Drawings'), ('loan_repayment', 'Loan Repayment'), ('other_payment', 'Other Payment'), ('transfer', 'Account Transfer'), ('cash_adjustment', 'Cash Adjustment'), ('customer_credit_note', 'Customer Credit Note'), ('customer_debit_note', 'Customer Debit Note'), ('supplier_credit_note', 'Supplier Credit Note'), ('supplier_debit_note', 'Supplier Debit Note'), ('loan_given', 'Loan Given'), ('loan_recovered', 'Loan Recovered'), ('set_off_receivable', 'Set-off (Receivable)'), ('set_off_payable', 'Set-off (Payable)')], max_length=256),
        ),
        migrations.AddField(
            model_name='partypayment',
            name='person',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, related_name='party_payments', to='root.person'),
        ),
        migrations.AddField(
            model_name='partypayment',
            name='loan',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='repayments', to='accounts.transaction'),
        ),
    ]
