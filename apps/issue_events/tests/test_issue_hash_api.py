from django.urls import reverse
from model_bakery import baker

from glitchtip.test_utils.test_case import GlitchTestCase


class IssueAPITestCase(GlitchTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.create_user()

    def setUp(self):
        self.client.force_login(self.user)

    def test_list_issue_hashes(self):
        issue_hash = baker.make(
            "issue_events.IssueHash", project=self.project, issue__project=self.project
        )
        baker.make(
            "issue_events.IssueEvent",
            issue=issue_hash.issue,
            data={"hashes": [issue_hash.value.hex]},
        )
        list_url = reverse(
            "api:list_issue_hashes",
            kwargs={
                "organization_slug": self.organization.slug,
                "issue_id": issue_hash.issue_id,
            },
        )
        res = self.client.get(list_url)
        self.assertEqual(res.json()[0]["id"], issue_hash.value.hex)
