"""Offline regressions: shared paths, credentials, and validated persistence."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(os.environ.get('LITERATURE_TEST_SOURCE', ROOT / 'src'))))
if os.environ.get('LITERATURE_TEST_SOURCE'):
    # A source-only patch can be checked before installing it.
    import providers
    for name, relative in [('providers.institutional_access', 'providers/institutional_access.py'),
                           ('literature_download_cli', 'literature_download_cli.py')]:
        spec = importlib.util.spec_from_file_location(name, ROOT / 'src' / relative)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)

import literature_download_cli as cli
from providers import institutional_access as access
from providers import webvpn_login_cdp as login


class WebVPNSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.entry = access.SchoolEntry(
            id='hhu', name='河海大学', province='', access_type='webvpn',
            base_url='https://webvpn.example.edu.cn', cookie_file_name='webvpn_cookies.json',
            auto_login={'username':'#username','password':'#password','submit':'#submit'})
        self.raw = {'institution': {'school_id':'hhu','school_name':'河海大学'},
                    'webvpn': {'enabled':True}}
        self.env = patch.dict(os.environ, {'LITERATURE_INSTITUTION_USERNAME':'',
                                          'LITERATURE_INSTITUTION_PASSWORD':''})
        self.env.start()
        self.addCleanup(self.env.stop)
        state = patch.object(access, 'application_state_dir', return_value=self.folder)
        state.start()
        self.addCleanup(state.stop)

    def merged(self, config):
        path = self.folder / 'config.json'
        path.write_text(json.dumps(config), encoding='utf-8')
        return cli.load_config(path)

    def test_gui_and_cli_choose_new_school_session_over_stale_legacy(self):
        legacy = self.folder / 'webvpn_cookies.json'
        modern = self.folder / 'schools' / 'webvpn_cookies.json'
        modern.parent.mkdir()
        legacy.write_text('[]')
        modern.write_text('[]')
        for cfg in [self.raw, self.merged(self.raw)]:
            self.assertEqual(access.cookie_file_for(self.entry, cfg), modern)

    def test_old_install_and_new_install_have_identical_gui_cli_paths(self):
        for old_file_exists in [False, True]:
            if old_file_exists:
                (self.folder / 'webvpn_cookies.json').write_text('[]')
            expected = self.folder / ('webvpn_cookies.json' if old_file_exists
                                      else 'schools/webvpn_cookies.json')
            for cfg in [self.raw, self.merged(self.raw)]:
                self.assertEqual(access.cookie_file_for(self.entry, cfg), expected)

    def test_explicit_paths_are_relative_to_application_not_cwd(self):
        from suite_paths import APP_DIR
        old_cwd = Path.cwd()
        try:
            os.chdir(self.folder)
            for section in ['institution', 'webvpn']:
                cfg = copy.deepcopy(self.raw)
                cfg[section]['cookie_file'] = 'state/custom.json'
                for value in [cfg, self.merged(cfg)]:
                    self.assertEqual(access.cookie_file_for(self.entry, value),
                                     (APP_DIR / 'state/custom.json').resolve())
        finally:
            os.chdir(old_cwd)

    def test_refresh_preserves_custom_path_but_switch_clears_school_secrets(self):
        cfg = copy.deepcopy(self.raw)
        cfg['institution'].update(cookie_file='state/custom.json',
                                  login_username='test-user', login_password='test-secret')
        same = access.apply_school_selection(copy.deepcopy(cfg), self.entry, self.folder/'schools.json')
        self.assertEqual(same['institution']['cookie_file'], 'state/custom.json')
        other = access.SchoolEntry('other','Other','','webvpn')
        changed = access.apply_school_selection(cfg, other, self.folder/'schools.json')
        for key in ['cookie_file', 'login_username', 'login_password']:
            self.assertNotIn(key, changed['institution'])

    def store(self):
        cred = SimpleNamespace(CRED_TYPE_GENERIC=1, CredRead=Mock(return_value={
            'UserName':'stored-user', 'CredentialBlob':'stored-secret'.encode('utf-16-le')}))
        modules = patch.dict(sys.modules, {'win32cred':cred, 'pywintypes':SimpleNamespace(error=OSError)})
        modules.start()
        self.addCleanup(modules.stop)
        return cred

    def test_json_pair_is_used_instead_of_stale_windows_credentials(self):
        store = self.store()
        cfg = copy.deepcopy(self.raw)
        cfg['institution'].update(login_username='json-user',login_password='json-secret')
        self.assertEqual(access.resolve_institution_credentials(cfg,self.entry),
                         ('json-user','json-secret','config'))
        self.assertEqual(access.auto_fill_settings(self.entry,cfg)['account'],'json-user')
        store.CredRead.assert_not_called()

    def test_readiness_and_fill_both_use_windows_fallback(self):
        self.store()
        with patch.object(access,'selected_school',return_value=(self.entry,self.folder,[])), \
             patch.object(cli,'selected_school',return_value=(self.entry,self.folder,[])), \
             patch.object(cli,'institution_session_status',return_value={'valid':False}):
            readiness = cli.redacted_readiness(self.folder/'config.json',self.raw)
            flags = readiness['providers']['institution']['auto_login']
            self.assertTrue(flags['account_configured'])
            self.assertTrue(flags['fills_form'])
            self.assertEqual(flags['credential_source'],'windows')
            self.assertEqual(access.auto_fill_settings(self.entry,self.raw)['account'],'stored-user')

    def test_partial_pair_never_borrows_another_sources_password(self):
        self.store()
        cfg = copy.deepcopy(self.raw)
        cfg['institution']['login_username'] = 'partial-user'
        self.assertIsNone(access.auto_fill_settings(self.entry,cfg))
        cfg['institution']['login_password'] = 'json-secret'
        with patch.dict(os.environ,{'LITERATURE_INSTITUTION_USERNAME':'env-user'}):
            self.assertIsNone(access.auto_fill_settings(self.entry,cfg))

    def test_dialog_override_precedes_environment(self):
        cfg = copy.deepcopy(self.raw)
        cfg['institution']['_setup_credentials'] = ('new-user','new-secret')
        with patch.dict(os.environ,{'LITERATURE_INSTITUTION_USERNAME':'env-user',
                                   'LITERATURE_INSTITUTION_PASSWORD':'env-secret'}):
            fill = access.auto_fill_settings(self.entry,cfg)
            self.assertEqual((fill['account'],fill['secret']),('new-user','new-secret'))

    def test_missing_optional_windows_module_is_safe(self):
        with patch.dict(sys.modules,{'win32cred':None}):
            self.assertIsNone(access.auto_fill_settings(self.entry,self.raw))

    def run_login(self, valid):
        cookies = [{'name':'session','value':'synthetic-test-value',
                    'domain':'webvpn.example.edu.cn','path':'/'}]
        cdp = Mock()
        cdp.call.return_value = {'cookies':cookies}
        window = Mock(cdp=cdp)
        window.alive.return_value = True
        context = Mock()
        context.__enter__ = Mock(return_value=window)
        context.__exit__ = Mock(return_value=False)
        clock = [0.0]
        def advance(seconds):
            clock[0] += seconds
        target = self.folder / 'saved.json'
        target.write_text('[{"old":"session"}]')
        probes = []
        def validate(path):
            probes.append(json.loads(path.read_text('utf-8')))
            return {'valid':valid,'message':'synthetic probe'}
        with patch.object(login,'application_state_dir',return_value=self.folder), \
             patch.object(login,'launch_profile_browser',return_value=context), \
             patch.object(login.time,'monotonic',side_effect=lambda:clock[0]), \
             patch.object(login.time,'sleep',side_effect=advance):
            if valid:
                result = login.login_with_installed_browser(base_url=self.entry.base_url,
                    cookie_file=target,timeout=12,validator=validate,browser=Path('test-browser.exe'))
                self.assertTrue(result['ok'])
                self.assertEqual(json.loads(target.read_text('utf-8')),cookies)
                self.assertGreaterEqual(len(probes),2)
            else:
                with self.assertRaises(login.BrowserLoginError):
                    login.login_with_installed_browser(base_url=self.entry.base_url,
                        cookie_file=target,timeout=12,validator=validate,browser=Path('test-browser.exe'))
                self.assertEqual(json.loads(target.read_text('utf-8')),[{'old':'session'}])
        self.assertFalse((self.folder/'webvpn_cookies.candidate.json').exists())

    def test_verified_login_saves_cookies(self):
        self.run_login(True)

    def test_invalid_login_preserves_previous_cookies(self):
        self.run_login(False)


if __name__ == '__main__':
    unittest.main(verbosity=2)
