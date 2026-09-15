"""桌面入口的持久化和撤销回归检查；全部数据写入临时目录。"""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('workbench_host', ROOT / 'platform.py')
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)
from lib import desktop_support as support


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='desktop-regression-')
        self.root = Path(self.temp.name)
        self.patches = []
        for key, value in {
            'HOME': str(self.root), 'DESKTOP_FILE': str(self.root / 'desktop.json'),
            'REGISTRY': str(self.root / 'registry.json'),
            'FEATURES_DIR': str(self.root / 'features'),
            'PREFFILE': str(self.root / 'prefs.json'),
            'LOGFILE': str(self.root / 'platform.log'),
        }.items():
            p = patch.object(host, key, value)
            p.start()
            self.patches.append(p)
        host._desktop_undo.clear()
        for dirname in ('features', 'store'):
            for fid in ('alpha', 'beta'):
                directory = self.root / dirname / fid
                directory.mkdir(parents=True)
                (directory / 'manifest.json').write_text(json.dumps({
                    'id': fid, 'name': fid, 'version': '1', 'platform': {},
                    'type': 'static', 'ui': {'entry': 'index.html'}}))
        host.register(str(self.root / 'features' / 'alpha'))
        host.set_ignored('beta')
        self.folder = self.root / '中文 空格'
        self.folder.mkdir()
        (self.folder / '保留.txt').write_text('valuable')

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def add(self, target, name=None):
        ok, message, item = host.add_desktop_item('auto', str(target), name)
        self.assertTrue(ok, message)
        return item

    def test_infer_folder_file_app_and_file_url(self):
        self.assertEqual(self.add(self.folder)['kind'], 'folder')
        self.assertEqual(self.add(self.folder / '保留.txt')['kind'], 'file')
        app = self.root / 'Sample.app'
        app.mkdir()
        self.assertEqual(self.add(app)['name'], 'Sample')
        self.assertEqual(support.resolve_target(self.folder.as_uri())['target'], str(self.folder))

    def test_invalid_protocol_and_missing_path_do_not_write(self):
        for value in ['javascript:alert(1)', 'data:text/plain,hi', 'ftp://example.com',
                      'https://u:secret@example.com', str(self.root / 'missing'), 'not a url']:
            self.assertFalse(host.add_desktop_item('auto', value)[0], value)
        self.assertEqual(host.load_desktop()['items'], [])

    def test_url_normalization_and_duplicate(self):
        item = self.add('EXAMPLE.com')
        self.assertEqual(item['target'], 'https://example.com/')
        self.assertEqual(item['name'], 'example.com')
        self.assertFalse(host.add_desktop_item('url', 'https://example.com/')[0])

    def test_hide_tool_keeps_registration_process_and_data(self):
        with patch.object(host, 'stop_feature') as stop:
            ok, _, token = host.remove_desktop_item('alpha')
            self.assertTrue(ok)
            stop.assert_not_called()
        self.assertEqual([x['id'] for x in host.all_features()], ['alpha'])
        self.assertEqual(host.desktop_items(), [])
        tool = next(x for x in host.desktop_tools() if x['id'] == 'alpha')
        self.assertTrue(tool['installed'])
        self.assertFalse(tool['on_desktop'])
        self.assertTrue(host.restore_desktop_item(token)[0])
        self.assertEqual(host.desktop_items()[0]['id'], 'alpha')

    def test_undo_preserves_file_identity_name_and_order(self):
        folder = self.add(self.folder, '我的资料')
        self.add('https://example.com')
        before = [x['id'] for x in host.desktop_items()]
        ok, _, token = host.remove_desktop_item(folder['id'])
        self.assertTrue(ok)
        self.assertEqual((self.folder / '保留.txt').read_text(), 'valuable')
        self.assertTrue(host.restore_desktop_item(token)[0])
        self.assertEqual([x['id'] for x in host.desktop_items()], before)
        self.assertEqual(host.load_desktop()['items'][0], folder)
        self.assertFalse(host.restore_desktop_item(token)[0])

    def test_expired_undo_and_repeated_hide(self):
        _, _, token = host.remove_desktop_item('alpha')
        self.assertFalse(host.remove_desktop_item('alpha')[0])
        host._desktop_undo[token]['expires'] = 0
        self.assertFalse(host.restore_desktop_item(token)[0])
        self.assertTrue(host.show_desktop_feature('alpha')[0])

    def test_add_from_library_reuses_existing_feature_files(self):
        marker = self.root / 'features/beta/data.json'
        marker.write_text('user data')
        self.assertTrue(host.show_desktop_feature('beta')[0])
        self.assertEqual(marker.read_text(), 'user data')
        self.assertIn('beta', [x['id'] for x in host.desktop_items()])
        self.assertNotIn('beta', host.ignored_ids())

    def test_edit_preserves_id_and_old_path_can_be_added_again(self):
        item = self.add('https://example.com/a')
        self.assertTrue(host.edit_desktop_item(item['id'], '新名称', 'https://example.com/b')[0])
        old = self.add('https://example.com/a')
        self.assertNotEqual(item['id'], old['id'])
        self.assertFalse(host.edit_desktop_item(old['id'], '重复', 'https://example.com/b')[0])
        self.assertEqual(host.load_desktop()['items'][0]['name'], '新名称')

    def test_feature_alias_does_not_modify_manifest(self):
        manifest = self.root / 'features/alpha/manifest.json'
        before = manifest.read_text()
        self.assertTrue(host.edit_desktop_item('alpha', '常用工具')[0])
        self.assertEqual(host.desktop_items()[0]['name'], '常用工具')
        self.assertEqual(manifest.read_text(), before)

    def test_sort_is_persisted_and_rejects_stale_or_invalid_order(self):
        self.add(self.folder)
        ids = [x['id'] for x in host.desktop_items()]
        self.assertTrue(host.reorder_desktop(list(reversed(ids)))[0])
        self.assertEqual([x['id'] for x in host.desktop_items()], list(reversed(ids)))
        for invalid in [[], [ids[0], ids[0]], ['missing'], [None], [{}]]:
            self.assertFalse(host.reorder_desktop(invalid)[0])

    def test_preview_title_and_offline_fallback(self):
        from email.message import Message
        headers = Message()
        headers['Content-Type'] = 'text/html; charset=utf-8'
        from unittest.mock import MagicMock
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = headers
        response.read.return_value = '<title>测试 &amp; 文档</title>'.encode()
        with patch.object(support.urllib.request, 'urlopen', return_value=response):
            self.assertEqual(support.preview_target('example.com')['name'], '测试 & 文档')
        with patch.object(support.urllib.request, 'urlopen', side_effect=OSError):
            self.assertEqual(support.preview_target('example.com')['name'], 'example.com')

    def test_native_cancel_returns_without_adding(self):
        from subprocess import CompletedProcess
        before = host.load_desktop()
        with patch.object(support.subprocess, 'run', return_value=CompletedProcess(
                [], 0, '{"ok":true,"cancelled":true,"paths":[]}', '')):
            self.assertTrue(support.native_pick('folder')['cancelled'])
        self.assertEqual(host.load_desktop(), before)


if __name__ == '__main__':
    unittest.main()
