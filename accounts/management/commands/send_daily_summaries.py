from django.core.management.base import BaseCommand
from django.utils import timezone

from core.utils import send_marketpro_email
from root.models import Business
from accounts.utils import daily_summary, parse_date


class Command(BaseCommand):

    help = (
        "Emails the daily accounting summary to the owner of every business "
        "with the accounting module enabled. The summary goes to the owner "
        "only, never to employees.\n\n"
        "There is no scheduler in the project, so run this from cron or "
        "Windows Task Scheduler at the end of each day:\n"
        "  python manage.py send_daily_summaries\n\n"
        "Pass --date to resend an earlier day, or --business-id to target one "
        "business."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--date',
            help='Day to summarise, as YYYY-MM-DD. Defaults to today.',
        )
        parser.add_argument(
            '--business-id',
            type=int,
            help='Send for a single business instead of all of them.',
        )

    def handle(self, *args, **options):
        day = parse_date(options.get('date'), timezone.localdate())

        businesses = Business.objects.filter(
            is_active=True, config__accounting=True
        ).select_related('owner', 'config')

        if options.get('business_id'):
            businesses = businesses.filter(id=options['business_id'])

        sent = 0
        skipped = 0

        for business in businesses:
            owner_email = getattr(business.owner, 'email', None)
            if not owner_email:
                skipped += 1
                self.stdout.write(self.style.WARNING(
                    f"{business.name}: owner has no email address, skipped."))
                continue

            try:
                summary = daily_summary(business.id, day)
                send_marketpro_email(
                    f"Daily Summary - {business.name} - {day.isoformat()}",
                    owner_email,
                    'emails/daily_summary.html',
                    {
                        'business': business,
                        'summary': summary,
                        'year': day.year,
                    },
                )
                sent += 1
                self.stdout.write(self.style.SUCCESS(
                    f"{business.name}: summary sent to {owner_email}."))
            except Exception as error:
                print(error)
                skipped += 1
                self.stdout.write(self.style.ERROR(
                    f"{business.name}: failed to send summary."))

        self.stdout.write(
            f"Done. {sent} sent, {skipped} skipped for {day.isoformat()}.")
