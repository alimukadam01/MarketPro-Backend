"""
Find, and optionally repair, purchase invoices whose stored total is stale.

Until the fix to PurchaseInvoice.adjust_totals, editing a purchase invoice's
lines (adding, removing or changing one) recomputed its total from the item
list as it was BEFORE the edit - a prefetch cache - and saved that. The update
screen showed the right figure, because it adds up in the browser; the stored
total, which the purchases list, the supplier's khaata and payables read, kept
the old one.

    python manage.py recompute_purchase_totals            # report only
    python manage.py recompute_purchase_totals --apply    # write the fixes
    python manage.py recompute_purchase_totals --business 3

Report-only unless --apply is given. The fix writes sub_total and total with a
queryset update, so no save signal runs: repairing a figure must not set off
restocking or anything else that hangs off saving an invoice.

The correct figures come from PurchaseInvoice.computed_totals - the same code
adjust_totals uses - so a repair cannot disagree with what a save produces.
"""
from django.core.management.base import BaseCommand
from django.db import connection, transaction

from sales.models import PurchaseInvoice

# Anything under half a paisa is float noise, not a stale total.
TOLERANCE = 0.005


class Command(BaseCommand):
    help = ("Report purchase invoices whose stored total no longer matches "
            "their lines; --apply to correct them.")

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='Write the corrected totals. Without it nothing is changed.')
        parser.add_argument(
            '--business', type=int, default=None,
            help='Only this business id.')

    def handle(self, *args, **options):
        apply = options['apply']
        # Said first and plainly: which database this is about to read, or
        # write. DEBUG decides it, and it differs between branches.
        db = connection.settings_dict
        self.stdout.write(
            f"Database: {connection.vendor} "
            f"({db.get('NAME')}{' @ ' + db['HOST'] if db.get('HOST') else ''})")
        self.stdout.write("Mode: " + ("APPLY - totals will be written"
                                      if apply else "report only - nothing is written"))

        invoices = PurchaseInvoice.objects.select_related('business', 'supplier')
        if options['business']:
            invoices = invoices.filter(business_id=options['business'])

        stale = []
        for invoice in invoices.order_by('business_id', 'id'):
            sub_total, total = invoice.computed_totals()
            if (abs((invoice.total or 0) - total) > TOLERANCE
                    or abs((invoice.sub_total or 0) - sub_total) > TOLERANCE):
                stale.append((invoice, sub_total, total))

        self.stdout.write(
            f"Checked {invoices.count()} purchase invoices; "
            f"{len(stale)} with a stale total.")

        for invoice, sub_total, total in stale:
            difference = total - (invoice.total or 0)
            self.stdout.write(
                f"  #{invoice.id} {invoice.invoice_number or '-'} | "
                f"{invoice.business.name} | "
                f"{invoice.supplier.name if invoice.supplier else '-'} | "
                f"stored {invoice.total or 0:,.2f} -> correct {total:,.2f} "
                f"({'+' if difference >= 0 else ''}{difference:,.2f})")

        if not apply or not stale:
            return

        with transaction.atomic():
            for invoice, sub_total, total in stale:
                PurchaseInvoice.objects.filter(pk=invoice.pk).update(
                    sub_total=sub_total, total=total)
        self.stdout.write(self.style.SUCCESS(f"Corrected {len(stale)} totals."))
