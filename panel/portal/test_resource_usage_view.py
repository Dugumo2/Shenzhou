"""统一统计发布后的读取保护；GET不改变周期状态。"""
from datetime import datetime,timezone,timedelta
import json
from unittest import TestCase
from unittest.mock import patch
from .resource_usage import project_resource_usage
from .resource_usage_view import read_resource_usage_view
from .test_resource_usage import PLANS,snapshot,timestamp


class ResourceUsageViewTests(TestCase):
    def setUp(self):
        self.now=datetime(2026,10,4,8,tzinfo=timezone.utc)
        _,self.dto=project_resource_usage(snapshot(self.now.timestamp()),PLANS,None,self.now.timestamp())

    def read(self,value=None,now=None):
        raw=json.dumps(self.dto if value is None else value).encode()
        with patch('portal.resource_usage_view._private_read',return_value=raw):
            return read_resource_usage_view('/fixture/view.json',now=now or self.now)

    def test_real_projected_dto_reads_estimated_remaining_and_independent_provider(self):
        result=self.read()
        self.assertNotIn('error',result)
        self.assertEqual(result['meters'][0]['remaining_bytes'],'399999999700')
        self.assertEqual(result['meters'][1]['remaining_bytes'],'9000')

    def test_expired_file_becomes_stale_without_rewriting_numbers(self):
        result=self.read(now=self.now+timedelta(minutes=4))
        self.assertEqual(result['meters'][0]['quality'],'stale')
        self.assertEqual(result['meters'][0]['used_bytes'],'300')
        self.assertEqual(result['meters'][1]['quality'],'current')

    def test_duplicate_keys_and_invalid_amount_fail_closed_without_paths(self):
        with patch('portal.resource_usage_view._private_read',return_value=b'{"schema_version":2,"schema_version":2}'):
            self.assertEqual(read_resource_usage_view('/private/secret')['meters'],[])
        self.dto['meters'][0]['used_bytes']='-1'
        result=self.read()
        self.assertEqual(result['meters'],[])
        self.assertNotIn('/fixture',json.dumps(result))

    def test_future_publication_explains_clock_error(self):
        result=self.read(now=self.now-timedelta(seconds=1))
        self.assertEqual(result['error']['code'],'usage_clock_error')

    def test_gap_does_not_hide_later_sample_expiration(self):
        self.dto['meters'][0]['quality']='gap'
        self.dto['meters'][0]['remaining_bytes']=None
        self.dto['meters'][0]['alerts']=[{'code':'collection_gap','severity':'warning','message':'缺少样本'}]
        result=self.read(now=self.now+timedelta(days=1))
        home=result['meters'][0]
        self.assertEqual(home['quality'],'gap')
        self.assertIsNone(home['remaining_bytes'])
        self.assertEqual({a['code'] for a in home['alerts']},{'collection_gap','source_stale'})

    def test_read_error_has_explicit_empty_state_and_no_exception_text(self):
        with patch('portal.resource_usage_view._private_read',side_effect=PermissionError('not-for-output')):
            result=read_resource_usage_view('/private/source')
        self.assertEqual(result['meters'],[])
        self.assertNotIn('not-for-output',json.dumps(result))
