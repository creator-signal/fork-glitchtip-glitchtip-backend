import json
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount, SocialApp
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection, transaction

from apps.organizations_ext.constants import OrganizationUserRole
from apps.organizations_ext.models import (
    Organization,
    OrganizationOwner,
    OrganizationSocialApp,
    OrganizationUser,
)
from apps.projects.models import Project, ProjectKey
from apps.teams.models import Team

PROVIDER_ID = "zitadel"
ORGANIZATION_SLUG = "creator-signal"
TEAM_SLUG = "creator-signal-operators"


@dataclass(frozen=True)
class ProjectDefinition:
    project_id: int
    name: str
    slug: str
    platform: str
    public_key: UUID
    dsn_file: str


PROJECTS = (
    ProjectDefinition(
        41401,
        "Sales Pulse web",
        "sales-pulse-web",
        "javascript-nextjs",
        UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"),
        "sales-pulse-web.dsn",
    ),
    ProjectDefinition(
        41402,
        "Sales Pulse worker",
        "sales-pulse-worker",
        "javascript-node",
        UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2"),
        "sales-pulse-worker.dsn",
    ),
    ProjectDefinition(
        41403,
        "Creator Signal public site",
        "creator-signal-public-site",
        "javascript-nextjs",
        UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3"),
        "creator-signal-public-site.dsn",
    ),
    ProjectDefinition(
        41404,
        "Creator Signal Strapi",
        "creator-signal-strapi",
        "javascript-node",
        UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa4"),
        "creator-signal-strapi.dsn",
    ),
    ProjectDefinition(
        41405,
        "Sales Pulse Admin browser",
        "sales-pulse-admin-browser",
        "javascript-nextjs",
        UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa5"),
        "sales-pulse-admin-browser.dsn",
    ),
    ProjectDefinition(
        41406,
        "Sales Pulse Admin server",
        "sales-pulse-admin-server",
        "javascript-node",
        UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa6"),
        "sales-pulse-admin-server.dsn",
    ),
)


def required_file(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as error:
        raise RuntimeError(f"required ZITADEL credential is missing: {path}") from error
    except PermissionError as error:
        raise RuntimeError(f"required ZITADEL credential is unreadable: {path}") from error
    if not value:
        raise RuntimeError(f"required ZITADEL credential is empty: {path}")
    return value


@dataclass(frozen=True)
class ReconcileConfig:
    client_id: str
    client_secret: str
    operator_email: str
    operator_subject: str
    discovery_url: str
    bootstrap_directory: Path

    @classmethod
    def from_environment(cls):
        credential_directory = Path(
            os.environ.get(
                "GLITCHTIP_ZITADEL_CREDENTIAL_DIRECTORY",
                "/run/zitadel",
            )
        )
        return cls(
            client_id=required_file(credential_directory / "client-id"),
            client_secret=required_file(credential_directory / "client-secret"),
            operator_email=required_file(credential_directory / "operator-email"),
            operator_subject=required_file(credential_directory / "operator-subject"),
            discovery_url=os.environ.get(
                "ZITADEL_DISCOVERY_URL",
                "https://auth.creatorsignal.me/.well-known/openid-configuration",
            ),
            bootstrap_directory=Path(
                os.environ.get("GLITCHTIP_BOOTSTRAP_DIRECTORY", "/run/bootstrap")
            ),
        )


def ensure_schema() -> None:
    if "socialaccount_socialapp" in connection.introspection.table_names():
        return
    call_command("migrate", interactive=False, verbosity=1)
    if "socialaccount_socialapp" not in connection.introspection.table_names():
        raise RuntimeError(
            "GlitchTip migrations did not create socialaccount_socialapp"
        )


def validate_runtime() -> None:
    required = {
        "CREATOR_SIGNAL_SSO_ONLY": True,
        "ENABLE_USER_REGISTRATION": False,
        "ENABLE_SOCIAL_APPS_USER_REGISTRATION": True,
        "ENABLE_ORGANIZATION_CREATION": False,
    }
    mismatches = [
        name for name, expected in required.items() if getattr(settings, name) is not expected
    ]
    if mismatches:
        raise RuntimeError(
            "unsafe Creator Signal GlitchTip authentication settings: "
            + ", ".join(mismatches)
        )


def write_private_file(path: Path, value: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(f"{value.rstrip()}\n", encoding="utf-8")
    temporary.chmod(0o400)
    temporary.replace(path)


def reconcile(config: ReconcileConfig) -> dict[str, object]:
    validate_runtime()
    ensure_schema()
    config.bootstrap_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    config.bootstrap_directory.chmod(0o700)

    with transaction.atomic():
        social_app, _ = SocialApp.objects.update_or_create(
            provider="openid_connect",
            provider_id=PROVIDER_ID,
            defaults={
                "name": "Creator Signal ZITADEL",
                "client_id": config.client_id,
                "secret": config.client_secret,
                "settings": {
                    "fetch_userinfo": True,
                    "oauth_pkce_enabled": True,
                    "server_url": config.discovery_url,
                    "token_auth_method": "client_secret_basic",
                    "uid_field": "sub",
                },
            },
        )

        user_model = get_user_model()
        user, _ = user_model.objects.get_or_create(
            email=config.operator_email,
            defaults={"is_active": True},
        )
        user.is_active = True
        user.is_staff = False
        user.is_superuser = False
        user.set_unusable_password()
        user.save(update_fields=["is_active", "is_staff", "is_superuser", "password"])
        EmailAddress.objects.update_or_create(
            user=user,
            email=config.operator_email,
            defaults={"verified": True, "primary": True},
        )

        subject_account = (
            SocialAccount.objects.select_for_update()
            .filter(provider=PROVIDER_ID, uid=config.operator_subject)
            .first()
        )
        if subject_account is not None and subject_account.user_id != user.id:
            raise RuntimeError(
                "ZITADEL operator subject is already linked to another GlitchTip user"
            )
        different_subject = (
            SocialAccount.objects.select_for_update()
            .filter(provider=PROVIDER_ID, user=user)
            .exclude(uid=config.operator_subject)
            .first()
        )
        if different_subject is not None:
            raise RuntimeError(
                "GlitchTip operator is already linked to a different ZITADEL subject"
            )
        SocialAccount.objects.update_or_create(
            provider=PROVIDER_ID,
            uid=config.operator_subject,
            defaults={
                "user": user,
                "extra_data": {"email": config.operator_email},
            },
        )

        organization, _ = Organization.objects.update_or_create(
            slug=ORGANIZATION_SLUG,
            defaults={
                "name": "Creator Signal",
                "open_membership": False,
                "is_deleted": False,
            },
        )
        organization_user, _ = OrganizationUser.objects.update_or_create(
            user=user,
            organization=organization,
            defaults={"role": OrganizationUserRole.OWNER},
        )
        OrganizationOwner.objects.update_or_create(
            organization=organization,
            defaults={"organization_user": organization_user},
        )
        OrganizationSocialApp.objects.update_or_create(
            social_app=social_app,
            defaults={"organization": organization},
        )

        team, _ = Team.objects.get_or_create(
            slug=TEAM_SLUG,
            organization=organization,
        )
        team.members.add(organization_user)

        reconciled_projects = []
        for definition in PROJECTS:
            project = Project.objects.filter(
                slug=definition.slug,
                organization=organization,
            ).first()
            if project is None:
                project = Project.objects.create(
                    id=definition.project_id,
                    slug=definition.slug,
                    organization=organization,
                    name=definition.name,
                    platform=definition.platform,
                )
            else:
                project.name = definition.name
                project.platform = definition.platform
                project.save(update_fields=["name", "platform"])
            team.projects.add(project)

            project_key = ProjectKey.objects.filter(project=project).order_by("id").first()
            if project_key is None:
                project_key = ProjectKey.objects.create(
                    project=project,
                    name="default",
                    public_key=definition.public_key,
                )
            else:
                project_key.name = "default"
                project_key.public_key = definition.public_key
                project_key.save(update_fields=["name", "public_key"])
            write_private_file(
                config.bootstrap_directory / definition.dsn_file,
                project_key.get_dsn(),
            )
            reconciled_projects.append(definition.slug)

    status = {
        "event": "glitchtip.reconciled",
        "provider_id": PROVIDER_ID,
        "organization": "Creator Signal",
        "operator_email": config.operator_email,
        "projects": reconciled_projects,
    }
    write_private_file(
        config.bootstrap_directory / "status.json",
        json.dumps(status, sort_keys=True),
    )
    return status
