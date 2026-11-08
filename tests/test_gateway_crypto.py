"""Keep the upstream gateway-key fix when integrating new session handling."""
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from providers import institutional_access as access


class GatewayCryptoTests(unittest.TestCase):
    def setUp(self):
        access._gateway_crypto_cache.clear()
        access._gateway_crypto_failures.clear()
        self.addCleanup(access._gateway_crypto_cache.clear)
        self.addCleanup(access._gateway_crypto_failures.clear)
        self.url = 'https://webvpn.example.edu.cn'

    def test_gateway_parameters_are_cached(self):
        session = MagicMock()
        html = "__vpn_host_crypt_key='0123456789abcdef'; __vpn_host_crypt_iv='fedcba9876543210';"
        with patch.object(access.requests, 'Session', return_value=session), \
             patch.object(access, '_response_html_preview', return_value=html):
            expected = (b'0123456789abcdef', b'fedcba9876543210')
            self.assertEqual(access.gateway_host_crypto(self.url), expected)
            self.assertEqual(access.gateway_host_crypto(self.url), expected)
        session.__enter__.return_value.get.assert_called_once()

    def test_invalid_parameters_back_off(self):
        session = MagicMock()
        with patch.object(access.requests, 'Session', return_value=session), \
             patch.object(access, '_response_html_preview', return_value="__vpn_host_crypt_key='short'"), \
             patch.object(access.time, 'monotonic', return_value=100):
            self.assertIsNone(access.gateway_host_crypto(self.url))
            self.assertIsNone(access.gateway_host_crypto(self.url))
        session.__enter__.return_value.get.assert_called_once()

    def test_client_uses_gateway_values_and_catalog_fallback(self):
        entry = access.SchoolEntry('example', 'Example', '', 'webvpn',
                                  base_url=self.url, key=b'catalog-key-1234', iv=b'catalog-key-1234')
        for detected in [(b'0123456789abcdef', b'fedcba9876543210'), None]:
            with self.subTest(detected=bool(detected)), \
                 patch.object(access, 'selected_school', return_value=(entry, Path('schools.json'), [])), \
                 patch.object(access, 'gateway_host_crypto', return_value=detected), \
                 patch.object(access, 'WebVPNClient') as client:
                access.make_institution_client({}, cookie_override=Path('candidate.json'))
                params = client.call_args.kwargs
                self.assertEqual((params['key'], params['iv']), detected or (entry.key, entry.iv))
                self.assertEqual(params['cookie_file'], Path('candidate.json'))

    def test_northwest_university_catalog_fix_is_preserved(self):
        entries, _ = access.load_school_catalog(Path(__file__).resolve().parents[1] / 'src/schools.json')
        school = next(e for e in entries if e.base_url == 'https://webvpn.nwu.edu.cn')
        self.assertEqual(school.key, b'nwuwyxisthebest!')
        self.assertEqual(school.iv, b'nwuwyxisthebest!')


if __name__ == '__main__':
    unittest.main(verbosity=2)
