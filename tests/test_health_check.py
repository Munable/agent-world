from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from agent_world.runtime_core import WorldRuntime
from tools.check_health import check_health


class HealthCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def client(self, payload):
        return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))

    def test_read_only_database_and_http_probe(self):
        database = self.root / 'world.sqlite3'
        WorldRuntime(database)
        with patch('tools.check_health.httpx.Client', return_value=self.client({'ok': True, 'universe': 'commons'})):
            result = check_health('http://127.0.0.1:8000', db=database)
        self.assertEqual(result['sqlite'], 'passed')
        with patch('tools.check_health.httpx.Client', return_value=self.client({'ok': True, 'universe': 'commons'})):
            self.assertEqual(check_health('http://127.0.0.1:8000')['sqlite'], 'not_checked')

    def test_missing_or_unrelated_database_is_not_created_or_accepted(self):
        for database in (self.root / 'missing.sqlite3', self.root / 'empty.sqlite3'):
            if database.name.startswith('empty'):
                database.touch()
            with patch('tools.check_health.httpx.Client', return_value=self.client({'ok': True, 'universe': 'commons'})):
                with self.assertRaises(Exception):
                    check_health('http://127.0.0.1:8000', db=database)
        self.assertFalse((self.root / 'missing.sqlite3').exists())

    def test_wrong_world_does_not_count_as_healthy(self):
        with patch('tools.check_health.httpx.Client', return_value=self.client({'ok': True, 'universe': 'other'})):
            with self.assertRaises(ValueError):
                check_health('http://127.0.0.1:8000')
