from allauth.socialaccount.models import SocialAccount
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.organizations_ext.models import OrganizationUser
from apps.teams.models import Team
from creativesignal.auth import sso_only_enabled


@receiver(
    post_save,
    sender=OrganizationUser,
    dispatch_uid="creativesignal.add_zitadel_user_to_operator_team",
)
def add_zitadel_user_to_operator_team(sender, instance, **kwargs):
    """Give an authorised ZITADEL user immediate access to governed projects."""
    del sender, kwargs
    if not sso_only_enabled() or instance.user_id is None:
        return
    if not SocialAccount.objects.filter(
        user_id=instance.user_id,
        provider="zitadel",
    ).exists():
        return

    team = Team.objects.filter(
        slug="creator-signal-operators",
        organization_id=instance.organization_id,
        organization__slug="creator-signal",
    ).first()
    if team is None:
        return
    team.members.add(instance)
