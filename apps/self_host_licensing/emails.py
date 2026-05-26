from datetime import datetime

from glitchtip.email import GlitchTipEmail


class SelfHostLicenseEmail(GlitchTipEmail):
    html_template_name = "self_host_licensing/license_email.html"
    text_template_name = "self_host_licensing/license_email.txt"
    subject_template_name = "self_host_licensing/license_email_subject.txt"


def send_license_email(
    to_email: str, blob: str, plan: str, expires_at: datetime
) -> None:
    email = SelfHostLicenseEmail(
        license_blob=blob,
        plan=plan,
        expires_at=expires_at,
    )
    email.send_email(to_email)
