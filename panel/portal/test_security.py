"""独立安全回归测试；使用临时文件测试库和公开假数据，不访问生产服务。"""

import atexit
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import uuid
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connections, transaction
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .forms import RegisterForm
from .models import AuditEvent, Invitation, Membership, OperationJob, RateBucket, SubscriptionGrant
from .services import invite_hash, register, token_for


class LocalTestDirectory:
    """使用工作区继承权限，避免 Windows tempfile 的 0700 特殊 ACL。"""

    def __init__(self, prefix):
        self.root = (settings.BASE_DIR / ".security-test-temp").resolve()
        self.root.mkdir(exist_ok=True)
        self.path = self.root / (prefix + uuid.uuid4().hex)
        self.path.mkdir()
        self.name = str(self.path)

    def cleanup(self):
        target = self.path.resolve()
        if target.parent != self.root or target == self.root:
            raise RuntimeError("test_cleanup_scope_invalid")
        if target.exists():
            shutil.rmtree(target)
        try:
            self.root.rmdir()
        except OSError:
            pass

    def __enter__(self):
        return self.name

    def __exit__(self, *args):
        self.cleanup()


# Django 默认共享内存 SQLite 的锁行为与生产文件库不同；测试 runner 在发现
# 本模块之后才创建测试库，因此只指定 TEST.NAME，不改变实际 NAME 或 var。
_database_directory = LocalTestDirectory(prefix="database-")
atexit.register(_database_directory.cleanup)
connections["default"].settings_dict.setdefault("TEST", {})["NAME"] = str(
    Path(_database_directory.name) / "security-test.sqlite3"
)

FAKE_PASSWORD = "Public-test-only-Password-482!"
FAST_PASSWORDS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


def invitation_for(creator, *, code="public-fake-invitation", max_uses=1, **extra):
    values = {
        "code_hash": invite_hash(code), "creator": creator, "max_uses": max_uses,
        "expires_at": timezone.now() + timedelta(hours=1), "quota_bytes": 100_000_000_000,
        "service_days": 30,
    }
    values.update(extra)
    return Invitation.objects.create(**values), code


def registration_form(username, code):
    form = RegisterForm({"username": username, "password1": FAKE_PASSWORD,
                         "password2": FAKE_PASSWORD, "invitation": code})
    if not form.is_valid():
        raise AssertionError("公开测试表单应通过前置校验")
    return form


@override_settings(PASSWORD_HASHERS=FAST_PASSWORDS)
class PortalSecurityTests(TestCase):
    def setUp(self):
        self.administrator = User.objects.create_user("test-admin", password=FAKE_PASSWORD, is_staff=True)
        self.alice = User.objects.create_user("alice", password=FAKE_PASSWORD)
        self.bob = User.objects.create_user("bob", password=FAKE_PASSWORD)
        self.storage = LocalTestDirectory(prefix="artifacts-")
        self.addCleanup(self.storage.cleanup)
        self.artifact_root = Path(self.storage.name) / "artifacts"
        self.snapshot_path = Path(self.storage.name) / "status.json"
        self.override = override_settings(ARTIFACT_ROOT=self.artifact_root, SNAPSHOT_PATH=self.snapshot_path,
                                         OPERATOR_ENABLED=False)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.alice_member = self.make_member(self.alice)
        self.bob_member = self.make_member(self.bob)

    def make_member(self, user):
        member = Membership.objects.create(user=user, status="active", provisioning_state="applied",
            quota_bytes=1_000_000, used_bytes=100, usage_state="measured", usage_updated_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=10))
        from bridge.artifacts import publish
        payloads = {}
        for device, name in (("windows", "windows.txt"), ("android", "android.json"), ("v2rayng", "v2rayng.txt")):
            SubscriptionGrant.objects.create(membership=member, device=device)
            payloads[device] = ("PUBLIC_FAKE_ARTIFACT_" + user.username + "_" + device).encode('utf8')
        publish(self.artifact_root, member.public_id, payloads)
        return member

    def download_url(self, member=None, device="windows"):
        grant = (member or self.alice_member).grants.get(device=device)
        return reverse("download", kwargs={"token": token_for(grant), "device": device})

    def read_download(self, response):
        # Django 测试客户端的包装迭代器负责关闭响应；重复 close 会额外发出
        # request_finished，在 TestCase 事务中错误关闭文件 SQLite 连接。
        return b"".join(response.streaming_content)

    def test_database_is_independent_temporary_file(self):
        actual = Path(connections["default"].settings_dict["NAME"]).resolve()
        self.assertTrue(actual.is_relative_to(Path(_database_directory.name).resolve()))
        self.assertNotEqual(actual, (settings.BASE_DIR / "var" / "panel.sqlite3").resolve())

    def test_anonymous_user_cannot_open_account_or_admin_pages(self):
        for path in ("/", "/account/", "/subscriptions/", "/manage/users/", "/manage/invites/", "/manage/operations/"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.url.startswith("/login/?next="))

    def test_normal_user_cannot_access_any_management_write_or_read(self):
        invitation, _ = invitation_for(self.administrator)
        self.client.force_login(self.alice)
        for path in ("/manage/users/", f"/manage/users/{self.bob.pk}/", "/manage/invites/",
                     f"/manage/invites/{invitation.pk}/revoke/", "/manage/operations/"):
            for method in ("get", "post"):
                with self.subTest(path=path, method=method):
                    response = getattr(self.client, method)(path, {"action": "backup", "confirm": "yes",
                                                                  "is_staff": "true", "user_id": self.administrator.pk})
                    self.assertEqual(response.status_code, 403)
        invitation.refresh_from_db()
        self.assertFalse(invitation.revoked)
        self.assertEqual(OperationJob.objects.count(), 0)

    def test_profile_ignores_privilege_and_other_user_fields(self):
        self.client.force_login(self.alice)
        response = self.client.post("/account/", {"action": "profile", "username": "alice-renamed",
            "is_staff": "true", "is_superuser": "true", "user_id": self.bob.pk, "id": self.bob.pk})
        self.assertEqual(response.status_code, 302)
        self.alice.refresh_from_db()
        self.bob.refresh_from_db()
        self.assertEqual(self.alice.username, "alice-renamed")
        self.assertFalse(self.alice.is_staff)
        self.assertFalse(self.alice.is_superuser)
        self.assertEqual(self.bob.username, "bob")

    def test_global_snapshot_is_admin_only_and_uses_field_allowlist(self):
        self.snapshot_path.write_text(json.dumps({"bwh_used_bytes": 73123456,
            "sing_box_status": "PUBLIC_FAKE_ADMIN_SNAPSHOT", "private_key": "PUBLIC_FAKE_FORBIDDEN_FIELD"}), encoding="utf-8")
        self.client.force_login(self.alice)
        response = self.client.get("/")
        self.assertEqual(response.context["snapshot"], {})
        self.assertNotContains(response, "PUBLIC_FAKE_ADMIN_SNAPSHOT")
        self.client.force_login(self.administrator)
        response = self.client.get("/")
        self.assertEqual(response.context["snapshot"]["bwh_used_bytes"], 73123456)
        self.assertNotIn("private_key", response.context["snapshot"])

    def test_csrf_blocks_all_unauthorized_browser_writes(self):
        strict = Client(enforce_csrf_checks=True)
        for path in ("/login/", "/register/"):
            with self.subTest(path=path):
                self.assertEqual(strict.post(path, {"username": "test", "password": FAKE_PASSWORD}).status_code, 403)
        strict.force_login(self.administrator)
        invitation, _ = invitation_for(self.administrator)
        for path in ("/logout/", "/account/", "/subscriptions/rotate/", "/manage/invites/",
                     f"/manage/invites/{invitation.pk}/revoke/", f"/manage/users/{self.alice.pk}/", "/manage/operations/"):
            with self.subTest(path=path):
                self.assertEqual(strict.post(path, {"confirm": "yes"}).status_code, 403)
        invitation.refresh_from_db()
        self.assertFalse(invitation.revoked)
        self.assertEqual(OperationJob.objects.count(), 0)

    def test_valid_csrf_token_does_not_allow_untrusted_origin(self):
        strict = Client(enforce_csrf_checks=True)
        strict.get("/login/")
        token = strict.cookies["csrftoken"].value
        response = strict.post("/login/", {"username": "alice", "password": FAKE_PASSWORD,
                              "csrfmiddlewaretoken": token}, HTTP_ORIGIN="https://attacker.example.invalid")
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("_auth_user_id", strict.session)

    def test_login_page_keeps_same_origin_referrer_for_csrf(self):
        response = self.client.get("/login/")
        self.assertEqual(response["Referrer-Policy"], "same-origin")
        self.assertNotContains(response, '<meta name="referrer"')

    def test_login_accepts_valid_csrf_and_correct_credentials(self):
        strict = Client(enforce_csrf_checks=True)
        strict.get("/login/")
        token = strict.cookies["csrftoken"].value
        response = strict.post("/login/", {"username": "alice", "password": FAKE_PASSWORD,
                              "csrfmiddlewaretoken": token}, HTTP_ORIGIN="http://testserver")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(int(strict.session["_auth_user_id"]), self.alice.pk)

    def exhaust_login_account(self):
        for _ in range(10):
            response = self.client.post("/login/", {"username": "alice", "password": "Public-wrong-password"})
            self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_login_rate_limit_blocks_correct_password_after_ten_attempts(self):
        self.exhaust_login_account()
        response = self.client.post("/login/", {"username": "alice", "password": FAKE_PASSWORD})
        self.assertIn(response.status_code, (200, 429))
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertContains(response, "频繁", status_code=response.status_code)

    def test_login_rate_limit_cannot_be_bypassed_by_username_whitespace(self):
        self.exhaust_login_account()
        response = self.client.post("/login/", {"username": " alice ", "password": FAKE_PASSWORD})
        self.assertIn(response.status_code, (200, 429))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_rate_limited_login_does_not_perform_authentication(self):
        self.exhaust_login_account()
        with patch("django.contrib.auth.forms.authenticate", return_value=None) as authenticate:
            response = self.client.post("/login/", {"username": "alice", "password": FAKE_PASSWORD})
        self.assertIn(response.status_code, (200, 429))
        authenticate.assert_not_called()

    def test_address_rate_limit_ignores_forged_forwarded_for(self):
        for number in range(30):
            self.client.post("/login/", {"username": f"public-missing-{number}", "password": "wrong"},
                             HTTP_X_FORWARDED_FOR=f"192.0.2.{number + 1}")
        response = self.client.post("/login/", {"username": "alice", "password": FAKE_PASSWORD},
                                    HTTP_X_FORWARDED_FOR="198.51.100.123")
        self.assertIn(response.status_code, (200, 429))
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertContains(response, "频繁", status_code=response.status_code)

    def test_invitation_maximum_uses_and_pending_membership(self):
        invitation, code = invitation_for(self.administrator, max_uses=2)
        for name in ("joined-one", "joined-two"):
            user = register(registration_form(name, code))
            member = Membership.objects.get(user=user)
            self.assertFalse(user.is_staff)
            self.assertFalse(user.is_superuser)
            self.assertFalse(member.eligible)
            self.assertEqual(member.status, "pending")
            self.assertIsNone(member.expires_at)
            self.assertEqual(member.grants.count(), 3)
        with self.assertRaises(ValidationError):
            register(registration_form("joined-three", code))
        invitation.refresh_from_db()
        self.assertEqual(invitation.uses, 2)
        self.assertFalse(User.objects.filter(username="joined-three").exists())
        self.assertEqual(AuditEvent.objects.filter(action="registered").count(), 2)

    def test_revoked_expired_and_unknown_invites_reject_without_creating_user(self):
        for index, extra in enumerate(({"revoked": True}, {"expires_at": timezone.now() - timedelta(seconds=1)})):
            invitation, code = invitation_for(self.administrator, code=f"public-invalid-{index}", **extra)
            with self.assertRaises(ValidationError):
                register(registration_form(f"not-created-{index}", code))
            invitation.refresh_from_db()
            self.assertEqual(invitation.uses, 0)
        with self.assertRaises(ValidationError):
            register(registration_form("not-created-unknown", "public-nonexistent-code"))
        self.assertFalse(User.objects.filter(username__startswith="not-created-").exists())

    def test_admin_revoke_is_post_only_and_prevents_later_registration(self):
        invitation, code = invitation_for(self.administrator)
        self.client.force_login(self.administrator)
        path = f"/manage/invites/{invitation.pk}/revoke/"
        self.assertEqual(self.client.get(path).status_code, 405)
        self.assertEqual(self.client.post(path).status_code, 302)
        with self.assertRaises(ValidationError):
            register(registration_form("revoked-registration", code))

    def test_own_subscription_page_ignores_other_membership_id(self):
        self.client.force_login(self.alice)
        response = self.client.get("/subscriptions/", {"user_id": self.bob.pk, "member": str(self.bob_member.public_id)})
        own_tokens = {token_for(grant) for grant in self.alice_member.grants.all()}
        self.assertEqual(len(response.context["links"]), 3)
        self.assertTrue(all(any(token in link["url"] for token in own_tokens) for link in response.context["links"]))
        for grant in self.bob_member.grants.all():
            self.assertNotContains(response, token_for(grant))

    def test_download_selects_token_owner_and_rejects_device_or_signature_tampering(self):
        own_url = self.download_url()
        response = self.client.get(own_url, {"user_id": self.bob.pk, "member": str(self.bob_member.public_id),
                                           "path": "../../forbidden"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.read_download(response), b"PUBLIC_FAKE_ARTIFACT_alice_windows")
        grant = self.alice_member.grants.get(device="windows")
        token = token_for(grant)
        for bad_device in ("android", "v2rayng", "unknown"):
            response = self.client.get(reverse("download", kwargs={"token": token, "device": bad_device}))
            self.assertEqual(response.status_code, 404)
        unsigned, signature = token.rsplit(":", 1)
        changed_signature = ("A" if signature[0] != "A" else "B") + signature[1:]
        self.assertEqual(self.client.get(reverse("download", kwargs={"token": unsigned + ":" + changed_signature,
                                                                     "device": "windows"})).status_code, 404)

    def test_rotation_only_changes_own_tokens_and_invalidates_old_downloads(self):
        alice_url, bob_url = self.download_url(), self.download_url(self.bob_member)
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get("/subscriptions/rotate/").status_code, 405)
        self.assertEqual(self.client.post("/subscriptions/rotate/", {}).status_code, 400)
        self.assertEqual(self.client.post("/subscriptions/rotate/", {"confirm": "yes", "user_id": self.bob.pk}).status_code, 302)
        self.assertEqual(self.client.get(alice_url).status_code, 404)
        response = self.client.get(self.download_url())
        self.assertEqual(response.status_code, 200)
        self.read_download(response)
        response = self.client.get(bob_url)
        self.assertEqual(response.status_code, 200)
        self.read_download(response)
        self.assertEqual(set(self.bob_member.grants.values_list("version", flat=True)), {1})
        self.assertEqual(set(self.alice_member.grants.values_list("version", flat=True)), {2})

    def assert_membership_denied(self, **changes):
        Membership.objects.filter(pk=self.alice_member.pk).update(**changes)
        response = self.client.get(self.download_url())
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("Subscription-Userinfo", response)
        self.client.force_login(self.alice)
        response = self.client.get("/subscriptions/")
        self.assertEqual(response.context["links"], [])

    def test_unapplied_quota_never_issues_subscription_even_with_artifact(self):
        self.assert_membership_denied(provisioning_state="pending_apply")

    def test_unmeasured_usage_never_issues_subscription_even_with_artifact(self):
        self.assert_membership_denied(usage_state="not_connected")

    def test_expired_suspended_exhausted_or_inactive_membership_is_denied(self):
        baseline = {"status": "active", "quota_bytes": 1_000_000, "used_bytes": 100,
                    "expires_at": timezone.now() + timedelta(days=10)}
        for changes in ({"status": "suspended"}, {"status": "pending"}, {"quota_bytes": 100},
                        {"used_bytes": 1_000_001}, {"expires_at": timezone.now() - timedelta(seconds=1)}, {"expires_at": None}):
            with self.subTest(changes=changes):
                Membership.objects.filter(pk=self.alice_member.pk).update(**baseline)
                self.assert_membership_denied(**changes)
        Membership.objects.filter(pk=self.alice_member.pk).update(**baseline)
        self.alice.is_active = False
        self.alice.save(update_fields=["is_active"])
        self.assertEqual(self.client.get(self.download_url()).status_code, 404)

    def test_missing_stale_or_far_future_usage_timestamp_fails_closed(self):
        for updated in (None, timezone.now() - timedelta(minutes=4), timezone.now() + timedelta(hours=1)):
            with self.subTest(updated=updated):
                self.assert_membership_denied(usage_updated_at=updated)

    def test_fresh_measurement_within_three_minutes_allows_download(self):
        Membership.objects.filter(pk=self.alice_member.pk).update(usage_updated_at=timezone.now() - timedelta(minutes=2))
        response = self.client.get(self.download_url())
        self.assertEqual(response.status_code, 200)
        self.read_download(response)

    def test_disabled_grant_or_missing_artifact_returns_404(self):
        url = self.download_url()
        self.alice_member.grants.filter(device="windows").update(enabled=False)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.alice_member.grants.filter(device="windows").update(enabled=True)
        (self.artifact_root / str(self.alice_member.public_id) / "current.json").unlink()
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_default_operator_cannot_queue_jobs(self):
        self.client.force_login(self.administrator)
        response = self.client.post("/manage/operations/", {"action": "backup", "confirm": "on"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(OperationJob.objects.count(), 0)
        self.assertContains(response, "尚未接入", status_code=409)
        self.assertNotContains(self.client.get("/manage/operations/"), "提交任务")

    def test_responses_are_not_cached_and_deny_framing(self):
        self.client.force_login(self.alice)
        for path in ("/", "/account/", "/subscriptions/", self.download_url()):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn("no-store", response["Cache-Control"])
                self.assertEqual(response["X-Frame-Options"], "DENY")
                self.assertIn("frame-ancestors 'none'", response["Content-Security-Policy"])
                if response.streaming:
                    self.read_download(response)


@override_settings(PASSWORD_HASHERS=FAST_PASSWORDS)
class InvitationConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.administrator = User.objects.create_user("concurrency-admin", password=FAKE_PASSWORD, is_staff=True)

    def redeem_concurrently(self, forms):
        barrier = threading.Barrier(len(forms))

        def redeem(form):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                try:
                    register(form)
                    return "created"
                except ValidationError:
                    return "rejected"
                except Exception as error:
                    return type(error).__name__
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=len(forms)) as workers:
            return list(workers.map(redeem, forms))

    def test_parallel_redemption_cannot_exceed_single_use(self):
        invitation, code = invitation_for(self.administrator)
        results = self.redeem_concurrently([registration_form("race-one", code), registration_form("race-two", code)])
        self.assertCountEqual(results, ["created", "rejected"])
        invitation.refresh_from_db()
        self.assertEqual(invitation.uses, 1)
        self.assertEqual(Membership.objects.count(), 1)

    def test_four_parallel_redemptions_respect_two_use_limit(self):
        invitation, code = invitation_for(self.administrator, max_uses=2)
        results = self.redeem_concurrently([registration_form(f"parallel-{i}", code) for i in range(4)])
        self.assertCountEqual(results, ["created", "created", "rejected", "rejected"])
        invitation.refresh_from_db()
        self.assertEqual(invitation.uses, 2)
        self.assertEqual(Membership.objects.count(), 2)

    def test_parallel_case_variants_cannot_create_duplicate_username(self):
        invitation, code = invitation_for(self.administrator, max_uses=2)
        results = self.redeem_concurrently([registration_form("Case-Race", code), registration_form("case-race", code)])
        self.assertCountEqual(results, ["created", "rejected"])
        self.assertEqual(User.objects.filter(username__iexact="case-race").count(), 1)
        invitation.refresh_from_db()
        self.assertEqual(invitation.uses, 1)

    def test_invitation_expiring_while_waiting_for_write_lock_is_rejected(self):
        invitation, code = invitation_for(self.administrator, expires_at=timezone.now() + timedelta(seconds=0.3))
        form = registration_form("expired-after-lock", code)
        started = threading.Event()

        def redeem():
            close_old_connections()
            try:
                started.set()
                try:
                    register(form)
                    return "created"
                except ValidationError:
                    return "rejected"
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=1) as workers:
            with transaction.atomic():
                Invitation.objects.select_for_update().get(pk=invitation.pk)
                result = workers.submit(redeem)
                self.assertTrue(started.wait(timeout=5))
                # 真实等待锁超过邀请码剩余有效期；不调整系统时钟。
                time.sleep(0.5)
            self.assertEqual(result.result(timeout=30), "rejected")
        invitation.refresh_from_db()
        self.assertEqual(invitation.uses, 0)

    def test_concurrent_revoke_allows_no_consumption_after_committed_revocation(self):
        invitation, code = invitation_for(self.administrator)
        form = registration_form("revoke-race", code)
        barrier = threading.Barrier(2)

        def redeem():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                try:
                    register(form)
                    return "created"
                except ValidationError:
                    return "rejected"
            finally:
                connections.close_all()

        def revoke():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                with transaction.atomic():
                    record = Invitation.objects.select_for_update().get(pk=invitation.pk)
                    record.revoked = True
                    record.save(update_fields=["revoked"])
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as workers:
            future_registration = workers.submit(redeem)
            future_revocation = workers.submit(revoke)
            result = future_registration.result(timeout=30)
            future_revocation.result(timeout=30)
        invitation.refresh_from_db()
        self.assertTrue(invitation.revoked)
        self.assertEqual(invitation.uses, 1 if result == "created" else 0)
        with self.assertRaises(ValidationError):
            register(registration_form("after-revoke-race", code))


class ProductionConfigurationTests(TestCase):
    def read_settings(self, **panel_environment):
        environment = {key: value for key, value in os.environ.items() if not key.startswith("PANEL_")}
        with LocalTestDirectory(prefix="settings-") as data_root:
            environment.update({"PANEL_DATA_ROOT": data_root, **panel_environment})
            program = (
                "import json,runpy,sys; "
                "v=runpy.run_path(sys.argv[1]); "
                "print(json.dumps({k:v[k] for k in ['PANEL_LIVE','OPERATOR_ENABLED',"
                "'ALLOWED_HOSTS','DEBUG','SESSION_COOKIE_SECURE','CSRF_COOKIE_SECURE','SECURE_SSL_REDIRECT']}))"
            )
            return subprocess.run([sys.executable, "-B", "-c", program, str(settings.BASE_DIR / "megabox" / "settings.py")],
                                  env=environment, capture_output=True, text=True, timeout=30)

    def test_default_configuration_is_local_with_operator_disabled(self):
        result = self.read_settings()
        self.assertEqual(result.returncode, 0)
        values = json.loads(result.stdout)
        self.assertFalse(values["PANEL_LIVE"])
        self.assertFalse(values["OPERATOR_ENABLED"])
        self.assertFalse(values["DEBUG"])
        self.assertNotIn("*", values["ALLOWED_HOSTS"])

    def test_live_without_strong_secret_is_rejected(self):
        result = self.read_settings(PANEL_LIVE="1", PANEL_HOSTS="panel.example.invalid")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ImproperlyConfigured", result.stderr)

    def test_live_without_explicit_hostname_is_rejected(self):
        result = self.read_settings(PANEL_LIVE="1", PANEL_SECRET_KEY="public-fake-testing-secret-" * 3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ImproperlyConfigured", result.stderr)

    def test_valid_live_configuration_has_https_and_operator_still_disabled(self):
        result = self.read_settings(PANEL_LIVE="1", PANEL_SECRET_KEY="public-fake-testing-secret-" * 3,
                                    PANEL_HOSTS="panel.example.invalid")
        self.assertEqual(result.returncode, 0)
        values = json.loads(result.stdout)
        self.assertTrue(values["PANEL_LIVE"])
        self.assertFalse(values["OPERATOR_ENABLED"])
        for option in ("SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE", "SECURE_SSL_REDIRECT"):
            self.assertTrue(values[option])

    def test_operator_cannot_be_enabled_in_non_live_environment(self):
        result = self.read_settings(PANEL_OPERATOR_ENABLED="1")
        if result.returncode:
            self.assertIn("ImproperlyConfigured", result.stderr)
        else:
            self.assertFalse(json.loads(result.stdout)["OPERATOR_ENABLED"])
