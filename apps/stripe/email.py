from glitchtip.email import GlitchTipEmail


class LicenseKeyEmail(GlitchTipEmail):
    """Sends a self-hosted user their license key (Stripe customer ID).

    Triggered from the public POST /api/0/billing/customer-by-email/ endpoint
    when a Stripe customer is found matching the submitted email.
    """

    html_template_name = "stripe/license-key.html"
    text_template_name = "stripe/license-key.txt"
    subject_template_name = "stripe/license-key-subject.txt"
    from_email = "GlitchTip Sales <sales@glitchtip.com>"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["license_key"] = self.kwargs["license_key"]
        return context
