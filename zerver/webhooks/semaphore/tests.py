from unittest.mock import patch

import orjson

from zerver.lib.test_classes import WebhookTestCase


class SemaphoreHookTests(WebhookTestCase):
    CHANNEL_NAME = "semaphore"
    URL_TEMPLATE = "/api/v1/external/semaphore?stream={stream}&api_key={api_key}"
    WEBHOOK_DIR_NAME = "semaphore"

    # Messages are generated by Semaphore on git push. The subject lines below
    # contain information on the repo and branch, and the message has links and
    # details about the build, deploy, server, author, and commit

    # Tests for Semaphore Classic
    def test_semaphore_build(self) -> None:
        expected_topic_name = "knighthood/master"  # repo/branch
        expected_message = """
[Build 314](https://semaphoreci.com/donquixote/knighthood/branches/master/builds/314) passed:
* **Commit**: [a490b8d508e: Create user account for Rocinante](https://github.com/donquixote/knighthood/commit/a490b8d508ebbdab1d77a5c2aefa35ceb2d62daf)
* **Author**: don@lamancha.com
""".strip()
        self.check_webhook(
            "build",
            expected_topic_name,
            expected_message,
            content_type="application/x-www-form-urlencoded",
        )

    def test_semaphore_deploy(self) -> None:
        expected_topic_name = "knighthood/master"
        expected_message = """
[Deploy 17](https://semaphoreci.com/donquixote/knighthood/servers/lamancha-271/deploys/17) of [build 314](https://semaphoreci.com/donquixote/knighthood/branches/master/builds/314) passed:
* **Commit**: [a490b8d508e: Create user account for Rocinante](https://github.com/donquixote/knighthood/commit/a490b8d508ebbdab1d77a5c2aefa35ceb2d62daf)
* **Author**: don@lamancha.com
* **Server**: lamancha-271
""".strip()
        self.check_webhook(
            "deploy",
            expected_topic_name,
            expected_message,
            content_type="application/x-www-form-urlencoded",
        )

    # Tests for Semaphore 2.0

    def test_semaphore2_push(self) -> None:
        expected_topic_name = "notifications/rw/webhook_impl"  # repo/branch
        expected_message = """
[Notifications](https://semaphore.semaphoreci.com/workflows/acabe58e-4bcc-4d39-be06-e98d71917703) pipeline **stopped**:
* **Commit**: [(2d9f5fcec1c)](https://github.com/renderedtext/notifications/commit/2d9f5fcec1ca7c68fa7bd44dd58ec4ff65814563) Implement webhooks for SemaphoreCI
* **Branch**: rw/webhook_impl
* **Author**: [radwo](https://github.com/radwo)
""".strip()
        self.check_webhook(
            "push", expected_topic_name, expected_message, content_type="application/json"
        )

    def test_semaphore2_push_non_gh_repo(self) -> None:
        expected_topic_name = "notifications/rw/webhook_impl"  # repo/branch
        expected_message = """
[Notifications](https://semaphore.semaphoreci.com/workflows/acabe58e-4bcc-4d39-be06-e98d71917703) pipeline **stopped**:
* **Commit**: (2d9f5fcec1c) Implement webhooks for SemaphoreCI
* **Branch**: rw/webhook_impl
* **Author**: radwo
""".strip()
        with patch("zerver.webhooks.semaphore.view.is_github_repo", return_value=False):
            self.check_webhook(
                "push", expected_topic_name, expected_message, content_type="application/json"
            )

    def test_semaphore_pull_request(self) -> None:
        expected_topic_name = "notifications/test-notifications"
        expected_message = """
[Notifications](https://semaphore.semaphoreci.com/workflows/84383f37-d025-4811-b719-61c6acc92a1e) pipeline **failed**:
* **Pull request**: [Testing PR notifications](https://github.com/renderedtext/notifications/pull/3)
* **Branch**: test-notifications
* **Author**: [radwo](https://github.com/radwo)
""".strip()
        self.check_webhook(
            "pull_request", expected_topic_name, expected_message, content_type="application/json"
        )

    def test_semaphore_pull_request_non_gh_repo(self) -> None:
        expected_topic_name = "notifications/test-notifications"
        expected_message = """
[Notifications](https://semaphore.semaphoreci.com/workflows/84383f37-d025-4811-b719-61c6acc92a1e) pipeline **failed**:
* **Pull request**: Testing PR notifications (#3)
* **Branch**: test-notifications
* **Author**: radwo
""".strip()
        with patch("zerver.webhooks.semaphore.view.is_github_repo", return_value=False):
            self.check_webhook(
                "pull_request",
                expected_topic_name,
                expected_message,
                content_type="application/json",
            )

    def test_semaphore_tag(self) -> None:
        expected_topic_name = "notifications"
        expected_message = """
[Notifications](https://semaphore.semaphoreci.com/workflows/a8704319-2422-4828-9b11-6b2afa3554e6) pipeline **stopped**:
* **Tag**: [v1.0.1](https://github.com/renderedtext/notifications/tree/v1.0.1)
* **Author**: [radwo](https://github.com/radwo)
""".strip()
        self.check_webhook(
            "tag", expected_topic_name, expected_message, content_type="application/json"
        )

    def test_semaphore_tag_non_gh_repo(self) -> None:
        expected_topic_name = "notifications"
        expected_message = """
[Notifications](https://semaphore.semaphoreci.com/workflows/a8704319-2422-4828-9b11-6b2afa3554e6) pipeline **stopped**:
* **Tag**: v1.0.1
* **Author**: radwo
""".strip()
        with patch("zerver.webhooks.semaphore.view.is_github_repo", return_value=False):
            self.check_webhook(
                "tag", expected_topic_name, expected_message, content_type="application/json"
            )

    def test_semaphore_unknown(self) -> None:
        expected_topic_name = "knighthood/master"
        expected_message = "unknown: passed"
        self.check_webhook(
            "unknown",
            expected_topic_name,
            expected_message,
            content_type="application/x-www-form-urlencoded",
        )

    def test_semaphore_unknown_event(self) -> None:
        expected_topic_name = "notifications"
        expected_message = """
[Notifications](https://semaphore.semaphoreci.com/workflows/a8704319-2422-4828-9b11-6b2afa3554e6) pipeline **stopped** for unknown event
""".strip()
        with patch(
            "zerver.webhooks.semaphore.tests.SemaphoreHookTests.get_body", self.get_unknown_event
        ):
            self.check_webhook(
                "tag", expected_topic_name, expected_message, content_type="application/json"
            )

    def get_unknown_event(self, fixture_name: str) -> str:
        """Return modified payload with revision.reference_type changed"""
        fixture_data = orjson.loads(
            self.webhook_fixture_data("semaphore", fixture_name, file_type="json")
        )
        fixture_data["revision"]["reference_type"] = "unknown"
        return fixture_data
