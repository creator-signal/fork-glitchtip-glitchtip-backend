from django.core.management.base import BaseCommand
from django.core.mail import send_mail
from django.conf import settings

class Command(BaseCommand):
    help = "Send a test email to verify SMTP settings"

    def add_arguments(self, parser):
        parser.add_argument("email", type=str, help="Email address to send test email to")

    def handle(self, *args, **options):
        recipient = options["email"]
        self.stdout.write(f"Sending test email to {recipient}...")
        self.stdout.write(f"SMTP Host: {settings.EMAIL_HOST}")
        self.stdout.write(f"SMTP Port: {settings.EMAIL_PORT}")
        self.stdout.write(f"SMTP User: {settings.EMAIL_HOST_USER}")
        self.stdout.write(f"SMTP USE_TLS: {settings.EMAIL_USE_TLS}")
        self.stdout.write(f"SMTP USE_SSL: {settings.EMAIL_USE_SSL}")
        self.stdout.write(f"SMTP Timeout: {settings.EMAIL_TIMEOUT}")

        try:
            send_mail(
                "GlitchTip Test Email",
                "This is a test email from GlitchTip to verify your SMTP settings.",
                settings.DEFAULT_FROM_EMAIL,
                [recipient],
                fail_silently=False,
            )
            self.stdout.write(self.style.SUCCESS(f"Successfully sent test email to {recipient}"))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Failed to send test email: {e}"))
            self.stdout.write("Hint: Check your EMAIL_URL or individual EMAIL_* settings in your environment.")
