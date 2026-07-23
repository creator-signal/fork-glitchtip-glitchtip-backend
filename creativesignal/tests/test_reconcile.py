import json
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlparse

from allauth.account.models import EmailAddress
from allauth.account.signals import user_logged_in
from allauth.socialaccount.models import SocialAccount, SocialApp
from django.test import RequestFactory, TestCase, override_settings

from apps.organizations_ext.constants import OrganizationUserRole
from apps.organizations_ext.models import (
    Organization,
    OrganizationSocialApp,
    OrganizationUser,
)
from apps.projects.models import Project, ProjectKey
from apps.teams.models import Team
from apps.users.models import User
from creativesignal.zitadel.reconcile import PROJECTS, ReconcileConfig, reconcile


@override_settings(
    CREATOR_SIGNAL_SSO_ONLY=True,
    ENABLE_USER_REGISTRATION=False,
    ENABLE_SOCIAL_APPS_USER_REGISTRATION=True,
    ENABLE_ORGANIZATION_CREATION=False,
    GLITCHTIP_URL=urlparse("http://localhost:48220"),
)
class ZitadelReconcileTestCase(TestCase):
    def config(self, directory: Path, secret: str = "initial-secret"):
        return ReconcileConfig(
            client_id="local-client",
            client_secret=secret,
            operator_email="operator@creatorsignal.test",
            operator_subject="zitadel-operator-1",
            discovery_url=(
                "http://auth.localhost:48080/.well-known/openid-configuration"
            ),
            bootstrap_directory=directory,
        )

    def test_reconcile_is_idempotent_and_rotates_the_secret(self):
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            first = reconcile(self.config(directory))
            second = reconcile(self.config(directory, "rotated-secret"))

            self.assertEqual(first, second)
            social_app = SocialApp.objects.get(
                provider="openid_connect",
                provider_id="zitadel",
            )
            self.assertEqual(social_app.secret, "rotated-secret")
            self.assertEqual(SocialApp.objects.count(), 1)

            organization = Organization.objects.get(slug="creator-signal")
            self.assertFalse(organization.open_membership)
            self.assertTrue(
                OrganizationSocialApp.objects.filter(
                    organization=organization,
                    social_app=social_app,
                ).exists()
            )
            operator = User.objects.get(email="operator@creatorsignal.test")
            self.assertFalse(operator.has_usable_password())
            self.assertTrue(
                EmailAddress.objects.filter(
                    user=operator,
                    email=operator.email,
                    verified=True,
                    primary=True,
                ).exists()
            )
            self.assertTrue(
                SocialAccount.objects.filter(
                    user=operator,
                    provider="zitadel",
                    uid="zitadel-operator-1",
                ).exists()
            )
            organization_user = OrganizationUser.objects.get(
                organization=organization,
                user=operator,
            )
            self.assertEqual(organization_user.role, OrganizationUserRole.OWNER)

            team = Team.objects.get(
                organization=organization,
                slug="creator-signal-operators",
            )
            self.assertIn(organization_user, team.members.all())
            self.assertEqual(Project.objects.filter(organization=organization).count(), 7)
            self.assertEqual(ProjectKey.objects.count(), 7)

            self.assertEqual(
                [(definition.project_id, definition.slug) for definition in PROJECTS],
                [
                    (41401, "sales-pulse-web"),
                    (41402, "sales-pulse-worker"),
                    (41403, "creator-signal-public-site"),
                    (41404, "creator-signal-strapi"),
                    (41405, "sales-pulse-admin-browser"),
                    (41406, "sales-pulse-admin-server"),
                    (41407, "sales-pulse-browser-extension"),
                ],
            )
            self.assertEqual(len({definition.public_key for definition in PROJECTS}), 7)
            self.assertEqual(len({definition.dsn_file for definition in PROJECTS}), 7)

            for definition in PROJECTS:
                project = Project.objects.get(
                    organization=organization,
                    slug=definition.slug,
                )
                self.assertIn(project, team.projects.all())
                dsn_path = directory / definition.dsn_file
                self.assertEqual(dsn_path.stat().st_mode & 0o777, 0o400)
                self.assertIn("@localhost:48220/", dsn_path.read_text())

            status = json.loads((directory / "status.json").read_text())
            self.assertEqual(status["event"], "glitchtip.reconciled")
            self.assertNotIn("rotated-secret", json.dumps(status))

    def test_operator_subject_conflict_fails_closed(self):
        with TemporaryDirectory() as temporary_directory:
            other_user = User.objects.create(email="other@creatorsignal.test")
            SocialAccount.objects.create(
                user=other_user,
                provider="zitadel",
                uid="zitadel-operator-1",
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "operator subject is already linked to another GlitchTip user",
            ):
                reconcile(self.config(Path(temporary_directory)))

    def test_authorized_zitadel_user_joins_operator_team(self):
        with TemporaryDirectory() as temporary_directory:
            reconcile(self.config(Path(temporary_directory)))
            user = User.objects.create(email="member@creatorsignal.test")
            SocialAccount.objects.create(
                user=user,
                provider="zitadel",
                uid="zitadel-user-1",
            )

            user_logged_in.send(
                sender=User,
                request=RequestFactory().get("/"),
                user=user,
            )

            organization_user = OrganizationUser.objects.get(user=user)
            self.assertEqual(organization_user.role, OrganizationUserRole.MEMBER)
            self.assertTrue(
                Team.objects.get(slug="creator-signal-operators")
                .members.filter(pk=organization_user.pk)
                .exists()
            )

    @override_settings(ENABLE_USER_REGISTRATION=True)
    def test_unsafe_runtime_settings_fail_closed(self):
        with TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(RuntimeError, "ENABLE_USER_REGISTRATION"):
                reconcile(self.config(Path(temporary_directory)))
