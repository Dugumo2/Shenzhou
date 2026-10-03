"""账号安全表单沿当前工作区导航；不写用户、密码或真实资源。"""
from django.contrib.auth.models import User
from django.test import RequestFactory, SimpleTestCase, override_settings

from .views import account


@override_settings(FRONTEND_ENABLED=True, PANEL_LIVE=False, WORKSPACE_V2=True, MONITOR_URL='')
class AccountNavigationTests(SimpleTestCase):
    def render_account(self, *, staff=False):
        request = RequestFactory().get('/account/')
        request.user = User(username='navigation-fixture', is_staff=staff)
        response = account(request)
        self.assertEqual(response.status_code, 200)
        return response.content.decode('utf-8')

    def test_owner_returns_to_current_workspace_without_legacy_subscription_instructions(self):
        content = self.render_account()
        for path in ('/app/#/services', '/app/#/guides', '/app/#/account'):
            self.assertIn(f'href="{path}"', content)
        self.assertNotIn('href="/subscriptions/"', content)
        self.assertNotIn('href="/guide/"', content)
        self.assertNotIn('/app/#/admin/', content)
        self.assertIn('联系管理员处理这份服务的链接与接入身份', content)
        self.assertIn('修改登录密码不会撤销旧节点身份', content)

    @override_settings(MONITOR_URL='https://monitor.example.invalid')
    def test_administrator_navigation_contains_only_connected_workspace_modules(self):
        content = self.render_account(staff=True)
        for name in ('overview', 'users', 'services', 'rules', 'servers', 'lines'):
            self.assertIn(f'href="/app/#/admin/{name}"', content)
        self.assertNotIn('href="/manage/', content)
        self.assertNotIn('资源容量', content)
        self.assertNotIn('服务运维', content)
        self.assertNotIn('https://monitor.example.invalid', content)

    def test_existing_password_form_and_post_target_are_preserved(self):
        content = self.render_account()
        self.assertIn('name="action" value="password"', content)
        self.assertIn('name="old_password"', content)
        self.assertIn('name="new_password1"', content)
        self.assertIn('name="new_password2"', content)
        self.assertIn('method="post"', content)
        self.assertIn('csrfmiddlewaretoken', content)

    @override_settings(FRONTEND_ENABLED=False)
    def test_frontend_disabled_preserves_existing_site_navigation(self):
        content = self.render_account(staff=True)
        self.assertIn('href="/subscriptions/"', content)
        self.assertIn('href="/account/"', content)
        self.assertIn('href="/manage/capacity/"', content)
        self.assertNotIn('href="/app/#/', content)

    @override_settings(PANEL_LIVE=True)
    def test_production_preserves_existing_site_navigation(self):
        content = self.render_account(staff=True)
        self.assertIn('href="/subscriptions/"', content)
        self.assertIn('href="/manage/capacity/"', content)
        self.assertNotIn('href="/app/#/', content)
