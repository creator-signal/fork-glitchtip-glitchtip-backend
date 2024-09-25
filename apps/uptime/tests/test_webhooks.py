import json
from datetime import datetime
from unittest import mock

from model_bakery import baker

from apps.alerts.constants import RecipientType
from apps.alerts.models import AlertRecipient
from glitchtip.test_utils.test_case import GlitchTipTestCase

from ..constants import MonitorType
from ..models import Monitor, MonitorCheck
from ..webhooks import send_uptime_as_webhook

TEST_URL = "https://burkesoftware.rocket.chat/hooks/Y8TttGY7RvN7Qm3gD/rqhHLiRSvYRZ8BhbhhhLYumdMksWnyj3Dqsqt8QKrmbNndXH"
DISCORD_TEST_URL = "https://discord.com/api/webhooks/not_real_id/not_real_token"
GOOGLE_CHAT_TEST_URL = "https://chat.googleapis.com/v1/spaces/space_id/messages?key=api_key&token=api_token"


class WebhookTestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()
        self.monitor = baker.make(Monitor, name="Example Monitor", url="https://example.com", monitor_type=MonitorType.GET, project=self.project)
        self.monitor_check = baker.make(MonitorCheck, monitor=self.monitor)

        self.expected_subject = "GlitchTip Uptime Alert"
        self.expected_message_down = "The monitored site has gone down."
        self.expected_message_up = "The monitored site is back up."

    @mock.patch("requests.post")
    def test_generic_webhook(self, mock_post):
        recipient = baker.make(AlertRecipient, recipient_type=RecipientType.GENERAL_WEBHOOK, url=TEST_URL)

        send_uptime_as_webhook(
            recipient,
            self.monitor_check.id,
            True,
            datetime.now(),
        )

        mock_post.assert_called_once()
        json_data = json.dumps(mock_post.call_args.kwargs["json"])
        self.assertIn(f'"text": "{self.expected_subject}"', json_data)
        self.assertIn(f'"title": "{self.monitor.name}"', json_data)
        self.assertIn(f'"text": "{self.expected_message_down}"', json_data)

        mock_post.reset_mock()

        send_uptime_as_webhook(
            recipient,
            self.monitor_check.id,
            False,
            datetime.now(),
        )

        mock_post.assert_called_once()
        json_data = json.dumps(mock_post.call_args.kwargs["json"])
        self.assertIn(f'"text": "{self.expected_subject}"', json_data)
        self.assertIn(f'"title": "{self.monitor.name}"', json_data)
        self.assertIn(f'"text": "{self.expected_message_up}"', json_data)

    @mock.patch("requests.post")
    def test_google_chat_webhook(self, mock_post):
        recipient = baker.make(AlertRecipient, recipient_type=RecipientType.GOOGLE_CHAT, url=GOOGLE_CHAT_TEST_URL)

        send_uptime_as_webhook(
            recipient,
            self.monitor_check.id,
            True,
            datetime.now(),
        )

        mock_post.assert_called_once()
        json_data = json.dumps(mock_post.call_args.kwargs["json"])
        self.assertIn(f'"title": "{self.expected_subject}", "subtitle": "{self.monitor.name}"', json_data)
        self.assertIn(f'"text": "{self.expected_message_down}"', json_data)

        mock_post.reset_mock()

        send_uptime_as_webhook(
            recipient,
            self.monitor_check.id,
            False,
            datetime.now(),
        )

        mock_post.assert_called_once()
        json_data = json.dumps(mock_post.call_args.kwargs["json"])
        self.assertIn(f'"title": "{self.expected_subject}", "subtitle": "{self.monitor.name}"', json_data)
        self.assertIn(f'"text": "{self.expected_message_up}"', json_data)

    @mock.patch("requests.post")
    def test_discord_webhook(self, mock_post):
        recipient = baker.make(AlertRecipient, recipient_type=RecipientType.DISCORD, url=DISCORD_TEST_URL)

        send_uptime_as_webhook(
            recipient,
            self.monitor_check.id,
            True,
            datetime.now(),
        )

        mock_post.assert_called_once()
        json_data = json.dumps(mock_post.call_args.kwargs["json"])
        self.assertIn(f'"content": "{self.expected_subject}"', json_data)
        self.assertIn(f'"title": "{self.monitor.name}", "description": "{self.expected_message_down}"', json_data)

        mock_post.reset_mock()

        send_uptime_as_webhook(
            recipient,
            self.monitor_check.id,
            False,
            datetime.now(),
        )

        mock_post.assert_called_once()
        json_data = json.dumps(mock_post.call_args.kwargs["json"])
        self.assertIn(f'"content": "{self.expected_subject}"', json_data)
        self.assertIn(f'"title": "{self.monitor.name}", "description": "{self.expected_message_up}"', json_data)
