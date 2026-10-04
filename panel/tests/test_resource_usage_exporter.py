"""导出器故障顺序与重复样本；只用内存替身，不读服务器路径。"""
from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch
from bridge import resource_usage_exporter as exporter
from portal.resource_usage import project_resource_usage
from portal.test_resource_usage import PLANS,snapshot,timestamp


class ExporterTests(TestCase):
    def setUp(self):
        self.now=timestamp('2026-10-04T16:00:00+08:00')
        self.saved={}

    def config(self,path,maximum=0):
        return {'schema_version':1,'plans':deepcopy(PLANS)} if path==exporter.PLANS else deepcopy(self.saved['state'])

    def test_view_failure_keeps_state_and_replay_does_not_double_charge(self):
        def write(path,value,mode,gid):
            if path==exporter.STATE:self.saved['state']=deepcopy(value)
            else:raise OSError('simulated view replacement failure')
            return 'hash'
        with patch.object(exporter,'protected_json',side_effect=self.config), patch.object(exporter.Path,'exists',return_value=False), patch.object(exporter.Path,'is_symlink',return_value=False), patch.object(exporter,'atomic_json',side_effect=write):
            with self.assertRaises(OSError):exporter.export_once(lambda *a,**k:snapshot(self.now),project_resource_usage,self.now)
        original=deepcopy(self.saved['state'])
        def recovered(path,value,mode,gid):
            self.saved['state' if path==exporter.STATE else 'view']=deepcopy(value)
            return 'hash'
        with patch.object(exporter,'protected_json',side_effect=self.config), patch.object(exporter.Path,'exists',return_value=True), patch.object(exporter.Path,'is_symlink',return_value=False), patch.object(exporter,'atomic_json',side_effect=recovered):
            result=exporter.export_once(lambda *a,**k:snapshot(self.now),project_resource_usage,self.now)
        self.assertEqual(result['status'],'PASS')
        self.assertEqual(self.saved['view']['meters'][0]['used_bytes'],'300')
        self.assertEqual(self.saved['state']['meters']['home']['baseline'],original['meters']['home']['baseline'])

    def test_unreadable_snapshot_never_replaces_either_file(self):
        with patch.object(exporter,'protected_json',side_effect=self.config), patch.object(exporter.Path,'exists',return_value=False), patch.object(exporter.Path,'is_symlink',return_value=False), patch.object(exporter,'atomic_json') as write:
            with self.assertRaises(ValueError):exporter.export_once(lambda *a,**k:{'state':'error'},project_resource_usage,self.now)
            write.assert_not_called()

    def test_duplicate_private_state_keys_are_rejected(self):
        with self.assertRaises(ValueError):exporter.unique([('meters',{}),('meters',{})])
