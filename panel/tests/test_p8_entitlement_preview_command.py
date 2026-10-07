"""预览命令权限和只读输出边界；业务归属由独立集成测试覆盖。"""
from io import StringIO
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

TARGET = 'portal.management.commands.p8_entitlement_preview'


@override_settings(PANEL_LIVE=False)
class P8PreviewCommandTests(SimpleTestCase):
    def invoke(self, **extra):
        output = StringIO()
        call_command('p8_entitlement_preview', actor_id=1,
                     p8_service_id='11111111-1111-4111-8111-111111111111',
                     entitlement_id=2, expected_revision=0, stdout=output, **extra)
        return json.loads(output.getvalue())

    @override_settings(PANEL_LIVE=True)
    def test_live_rejected_before_query_or_helper(self):
        with patch(TARGET + '.get_user_model') as users, patch(TARGET + '.preview_p8_entitlement') as helper:
            with self.assertRaises(CommandError):
                self.invoke()
            users.assert_not_called()
            helper.assert_not_called()

    def test_output_whitelist_cannot_dump_future_helper_secrets(self):
        with patch(TARGET + '.get_user_model') as users, patch(TARGET + '.preview_p8_entitlement') as helper:
            users.return_value.objects.filter.return_value.first.return_value = SimpleNamespace(pk=1)
            helper.return_value = {'p8_public_id': '11111111-1111-4111-8111-111111111111',
                'entitlement_id': 2, 'revision': 0, 'state': 'unverified', 'owner_id': 1,
                'changes': [{'secret': 'DO_NOT_LOG_THIS'}], 'source_config': 'DO_NOT_LOG_THIS'}
            result = self.invoke()
            self.assertFalse(result['writes'])
            self.assertTrue(result['would_create_binding'])
            self.assertNotIn('DO_NOT_LOG_THIS', json.dumps(result))
            self.assertNotIn('changes', result)
            self.assertEqual(helper.call_count, 1)

    def test_validation_exception_does_not_echo_raw_input(self):
        with patch(TARGET + '.get_user_model') as users, patch(TARGET + '.preview_p8_entitlement') as helper:
            users.return_value.objects.filter.return_value.first.return_value = SimpleNamespace(pk=1)
            helper.side_effect = ValidationError('DO_NOT_LOG_THIS')
            with self.assertRaises(CommandError) as error:
                self.invoke()
            self.assertNotIn('DO_NOT_LOG_THIS', str(error.exception))

    def test_missing_actor_never_reaches_helper(self):
        with patch(TARGET + '.get_user_model') as users, patch(TARGET + '.preview_p8_entitlement') as helper:
            users.return_value.objects.filter.return_value.first.return_value = None
            with self.assertRaises(CommandError):
                self.invoke()
            helper.assert_not_called()
