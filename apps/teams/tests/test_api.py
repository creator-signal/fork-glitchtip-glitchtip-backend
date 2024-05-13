from django.test import TestCase
from django.urls import reverse
from model_bakery import baker

from apps.organizations_ext.models import OrganizationUserRole

from ..models import Team


class TeamAPITestCase(TestCase):
    def setUp(self):
        self.user = baker.make("users.user")
        self.organization = baker.make("organizations_ext.Organization")
        self.org_user = self.organization.add_user(self.user)
        self.client.force_login(self.user)

    def test_retrieve(self):
        team = baker.make("teams.Team", organization=self.organization)
        url = reverse("api:get_team", args=[self.organization.slug, team.slug])
        res = self.client.get(url)
        self.assertContains(res, team.slug)

    def test_delete(self):
        team = baker.make("teams.Team", organization=self.organization)
        url = reverse("api:delete_team", args=[self.organization.slug, team.slug])
        res = self.client.delete(url)
        self.assertTrue(res.status_code, 204)
        self.assertFalse(Team.objects.exists())

        team = baker.make("teams.Team", organization=self.organization)
        self.org_user.role = OrganizationUserRole.MEMBER
        self.org_user.save()
        url = reverse("api:delete_team", args=[self.organization.slug, team.slug])
        res = self.client.delete(url)
        self.assertTrue(res.status_code, 404)
        self.assertTrue(Team.objects.exists())

    def test_update(self):
        team = baker.make("teams.Team", organization=self.organization)
        url = reverse("api:update_team", args=[self.organization.slug, team.slug])
        slug = "newslug"
        res = self.client.put(url, data={"slug": slug}, content_type="application/json")
        self.assertContains(res, slug)
        team.refresh_from_db()
        self.assertEqual(team.slug, slug)

    def test_list(self):
        url = reverse("api:list_teams", args=[self.organization.slug])
        team = baker.make("teams.Team", organization=self.organization)
        other_organization = baker.make("organizations_ext.Organization")
        other_organization.add_user(self.user)
        other_team = baker.make("teams.Team", organization=other_organization)
        res = self.client.get(url)
        self.assertContains(res, team.slug)
        self.assertNotContains(res, other_team.slug)

    def test_create(self):
        url = reverse("api:create_team", args=[self.organization.slug])
        data = {"slug": "team"}
        res = self.client.post(url, data, content_type="application/json")
        self.assertContains(res, data["slug"], status_code=201)
        self.assertTrue(Team.objects.filter(slug=data["slug"]).exists())

    def test_unauthorized_create(self):
        """Only admins can create teams for that org"""
        data = {"slug": "team"}
        organization = baker.make("organizations_ext.Organization")
        url = reverse("organization-teams-list", args=[organization.slug])
        res = self.client.post(url, data)
        # Not even in this org
        self.assertEqual(res.status_code, 400)

        admin_user = baker.make("users.user")
        organization.add_user(admin_user)  # First user is always admin
        organization.add_user(self.user)
        res = self.client.post(url, data)
        # Not an admin
        self.assertEqual(res.status_code, 400)

    def test_invalid_create(self):
        url = reverse("organization-teams-list", args=["haha"])
        data = {"slug": "team"}
        res = self.client.post(url, data)
        self.assertEqual(res.status_code, 400)

    def test_add_member_to_team(self):
        team = baker.make("teams.Team", organization=self.organization)
        org_user = baker.make(
            "organizations_ext.OrganizationUser", organization=self.organization
        )
        res = self.client.post(
            reverse(
                "api:add_member_to_team",
                args=[self.organization.slug, 9**9, team.slug],
            )
        )
        self.assertEqual(res.status_code, 404)
        self.assertEqual(team.members.count(), 0)

        res = self.client.post(
            reverse(
                "api:add_member_to_team",
                args=[self.organization.slug, "me", team.slug],
            )
        )
        self.assertEqual(res.status_code, 201)
        res = self.client.post(
            reverse(
                "api:add_member_to_team",
                args=[self.organization.slug, org_user.id, team.slug],
            )
        )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(team.members.count(), 2)

    def test_delete_member_from_team(self):
        team = baker.make(
            "teams.Team", organization=self.organization, members=[self.org_user]
        )
        res = self.client.delete(
            reverse(
                "api:delete_member_from_team",
                args=[self.organization.slug, "me", team.slug],
            )
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(team.members.count(), 0)


# class TeamTestCase(TestCase):
#     def setUp(self):
#         self.user = baker.make("users.user")
#         self.organization = baker.make("organizations_ext.Organization")
#         self.org_user = self.organization.add_user(self.user)
#         self.client.force_login(self.user)
#         self.url = reverse("team-list")

#     def test_list(self):
#         team = baker.make("teams.Team", organization=self.organization)
#         other_team = baker.make("teams.Team")
#         res = self.client.get(self.url)
#         self.assertContains(res, team.slug)
#         self.assertNotContains(res, other_team.slug)

#     def test_retrieve(self):
#         team = baker.make("teams.Team", organization=self.organization)
#         team.members.add(self.org_user)
#         url = reverse(
#             "team-detail",
#             kwargs={
#                 "pk": f"{self.organization.slug}/{team.slug}",
#             },
#         )
#         res = self.client.get(url)
#         self.assertContains(res, team.slug)
#         self.assertTrue(res.data["isMember"])

#     def test_invalid_retrieve(self):
#         team = baker.make("teams.Team")
#         url = reverse(
#             "team-detail",
#             kwargs={
#                 "pk": f"{self.organization.slug}/{team.slug}",
#             },
#         )
#         res = self.client.get(url)
#         self.assertEqual(res.status_code, 404)
