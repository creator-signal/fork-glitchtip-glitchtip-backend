from django.conf import settings
from django.contrib.auth import get_user_model

from glitchtip.email import GlitchTipEmail

User = get_user_model()


class AlertEmail(GlitchTipEmail):
    html_template_name = "alerts/issue.html"
    text_template_name = "alerts/issue.txt"
    subject_template_name = "alerts/issue-subject.txt"
    notification = None
    metadata_fields = []

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        notification = self.notification
        first_issue = notification.issues.all().first()
        base_url = settings.GLITCHTIP_URL.geturl()
        org_slug = first_issue.project.organization.slug
        issue_link = f"{base_url}/{org_slug}/issues/{first_issue.id}"
        settings_link = (
            f"{base_url}/{org_slug}/settings/projects/{first_issue.project.slug}"
        )
        context["issue_title"] = first_issue.title
        context["project_name"] = first_issue.project
        context["first_issue"] = first_issue
        context["issue_link"] = issue_link
        context["issues"] = notification.issues.all()
        context["issue_count"] = notification.issues.count()
        context["project_notification_settings_link"] = settings_link
        context["org_slug"] = org_slug
        context["project_link"] = (
            f"{base_url}/{org_slug}/issues?project={first_issue.project.id}"
        )

        metadata_values = {}
        if first_issue.metadata and self.metadata_fields:
            for key in self.metadata_fields:
                if key in first_issue.metadata:
                    metadata_values[key] = first_issue.metadata[key]
        context["metadata_values"] = metadata_values
        
        return context


def send_email_notification(notification, metadata_fields=None):
    email = AlertEmail()
    email.notification = notification
    email.metadata_fields = metadata_fields if metadata_fields is not None else []
    users = User.objects.alert_notification_recipients(notification)
    if not users.exists():
        return
    email.send_users_email(users)
