import unittest
from types import SimpleNamespace

from websocket_client import WebSocketClient, build_websocket_connect_url, build_websocket_origin


class WebSocketClientUrlTests(unittest.TestCase):
    def test_build_origin_for_ws_url(self):
        self.assertEqual(
            build_websocket_origin('ws://127.0.0.1:8000/ws/ui/actuator/'),
            'http://127.0.0.1:8000',
        )

    def test_build_origin_for_wss_url(self):
        self.assertEqual(
            build_websocket_origin('wss://example.com/ws/ui/actuator/'),
            'https://example.com',
        )

    def test_build_connect_url_adds_id_query_param(self):
        self.assertEqual(
            build_websocket_connect_url(
                'ws://127.0.0.1:8000/ws/ui/actuator/',
                'actuator-1',
            ),
            'ws://127.0.0.1:8000/ws/ui/actuator/?id=actuator-1',
        )

    def test_build_connect_url_preserves_existing_query(self):
        self.assertEqual(
            build_websocket_connect_url(
                'ws://127.0.0.1:8000/ws/ui/actuator/?lang=en',
                'actuator-1',
            ),
            'ws://127.0.0.1:8000/ws/ui/actuator/?lang=en&id=actuator-1',
        )

    def test_build_connect_url_replaces_existing_identity_query(self):
        self.assertEqual(
            build_websocket_connect_url(
                'ws://127.0.0.1:8000/ws/ui/actuator/?user_id=old&id=older&lang=en',
                'actuator-1',
            ),
            'ws://127.0.0.1:8000/ws/ui/actuator/?lang=en&id=actuator-1',
        )

    def test_build_connect_url_adds_registration_token(self):
        self.assertEqual(
            build_websocket_connect_url(
                'ws://127.0.0.1:8000/ws/ui/actuator/?lang=en',
                'actuator-1',
                'token-1',
            ),
            'ws://127.0.0.1:8000/ws/ui/actuator/?lang=en&id=actuator-1&token=token-1',
        )

    def test_build_actuator_info_payload_includes_browser_state(self):
        client = WebSocketClient(
            'ws://127.0.0.1:8000/ws/ui/actuator/',
            'actuator-1',
            SimpleNamespace(
                actuator_name='demo',
                browser_type='chromium',
                headless=True,
                registration_token='token-1',
                heartbeat_interval=15,
            ),
        )
        client._connected_at = '2026-01-01T00:00:00+00:00'

        payload = client._build_actuator_info_payload()

        self.assertEqual(payload['name'], 'demo')
        self.assertEqual(payload['browser_type'], 'chromium')
        self.assertTrue(payload['headless'])
        self.assertEqual(payload['actuator_id'], 'actuator-1')
        self.assertEqual(payload['connected_at'], '2026-01-01T00:00:00+00:00')


if __name__ == '__main__':
    unittest.main()
