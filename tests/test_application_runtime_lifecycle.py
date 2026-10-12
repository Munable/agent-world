"""App owners release persistent database handles; injected runtimes stay usable."""
import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_world.application import create_application
from agent_world.http_app import create_app
from agent_world.mcp_app import create_mcp_app, create_mcp_server
from agent_world.onboarding_app import create_onboarding_app
from agent_world.product_app import create_product_app
from agent_world.runtime_core import WorldRuntime


class ApplicationRuntimeLifecycleTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db=(Path(directory.name)/'world.sqlite3').resolve()

    def exit_lifespan(self,app,*,fail=False):
        async def run():
            async with app.router.lifespan_context(app):
                if fail:raise ValueError('application failed')
        if fail:
            with self.assertRaisesRegex(ValueError,'application failed'):asyncio.run(run())
        else:asyncio.run(run())

    def assert_closed(self,runtime):
        self.assertEqual(runtime._idle_connections,{False:[],True:[]})
        with self.assertRaisesRegex(RuntimeError,'closed'):
            with runtime._conn():pass

    def test_standalone_http_product_and_onboarding_close_owned_runtime(self):
        factories=[lambda: create_app(self.db,installer=lambda *_:None),
                   lambda: create_product_app(self.db),lambda: create_onboarding_app(self.db)]
        for factory in factories:
            for fail in (False,True):
                with self.subTest(factory=factory,fail=fail):
                    app=factory();runtime=app.state.runtime
                    self.addCleanup(runtime.close)
                    self.exit_lifespan(app,fail=fail)
                    self.assert_closed(runtime)

    def test_transport_does_not_close_injected_runtime(self):
        runtime=WorldRuntime(self.db);self.addCleanup(runtime.close)
        for app in (create_app(self.db,runtime=runtime),create_product_app(self.db,runtime=runtime)):
            self.exit_lifespan(app)
            with runtime._conn() as c:self.assertEqual(c.execute('SELECT 1').fetchone()[0],1)

    def test_standalone_mcp_closes_after_sdk_lifespan(self):
        app,_,runtime=create_mcp_app(self.db,auth_required=False,installer=lambda *_:None)
        self.addCleanup(runtime.close)
        self.exit_lifespan(app)
        self.assert_closed(runtime)

    def test_composed_application_closes_shared_runtime_on_shutdown(self):
        app=create_application(self.db,world_profile='demo',universe='demo',timers_enabled=False)
        self.addCleanup(app.state.runtime.close)
        self.exit_lifespan(app)
        self.assert_closed(app.state.runtime)

    def test_failed_http_installer_releases_runtime(self):
        handles=[]
        def install(runtime,universe):
            with runtime._conn() as c:handles.append(c)
            raise ValueError('installation failed')
        with self.assertRaisesRegex(ValueError,'installation failed'):
            create_app(self.db,installer=install)
        with self.assertRaises(sqlite3.ProgrammingError):handles[0].execute('SELECT 1')

    def capture_runtime(self, instances):
        def create(*args, **kwargs):
            runtime = WorldRuntime(*args, **kwargs)
            self.addCleanup(runtime.close)
            instances.append(runtime)
            return runtime
        return create

    def test_standalone_factory_failures_release_owned_runtime_before_lifespan(self):
        cases = [
            ('agent_world.http_app', 'WorldGateway', lambda: create_app(self.db, installer=lambda *_: None)),
            ('agent_world.onboarding_app', 'FastAPI', lambda: create_onboarding_app(self.db)),
            ('agent_world.mcp_app', 'Server', lambda: create_mcp_server(self.db, installer=lambda *_: None)),
        ]
        for module, failing_constructor, factory in cases:
            with self.subTest(module=module):
                instances = []
                with patch(module+'.WorldRuntime', side_effect=self.capture_runtime(instances)), \
                        patch(module+'.'+failing_constructor, side_effect=RuntimeError('factory failed')):
                    with self.assertRaisesRegex(RuntimeError, 'factory failed'):
                        factory()
                self.assertEqual(len(instances), 1)
                self.assert_closed(instances[0])

    def test_missing_product_assets_release_owner_but_leave_injected_runtime_usable(self):
        missing = self.db.parent/'missing-package-assets'
        instances = []
        with patch('agent_world.product_app.WorldRuntime', side_effect=self.capture_runtime(instances)), \
                patch('importlib.resources.files', return_value=missing):
            with self.assertRaisesRegex(RuntimeError, 'does not exist'):
                create_product_app(self.db)
        self.assert_closed(instances[0])
        injected = WorldRuntime(self.db)
        self.addCleanup(injected.close)
        with patch('importlib.resources.files', return_value=missing):
            with self.assertRaisesRegex(RuntimeError, 'does not exist'):
                create_product_app(self.db, runtime=injected)
        with injected._conn() as c:
            self.assertEqual(c.execute('SELECT 1').fetchone()[0], 1)

    def test_failed_http_factory_does_not_close_injected_runtime(self):
        runtime = WorldRuntime(self.db)
        self.addCleanup(runtime.close)
        with patch('agent_world.http_app.WorldGateway', side_effect=RuntimeError('gateway failed')):
            with self.assertRaisesRegex(RuntimeError, 'gateway failed'):
                create_app(self.db, runtime=runtime)
        with runtime._conn() as c:
            self.assertEqual(c.execute('SELECT 1').fetchone()[0], 1)

    def test_failed_mcp_sdk_app_releases_created_runtime(self):
        runtime = WorldRuntime(self.db)
        self.addCleanup(runtime.close)
        server = Mock()
        server.streamable_http_app.side_effect = RuntimeError('SDK app failed')
        with patch('agent_world.mcp_app.create_mcp_server', return_value=(server, runtime)):
            with self.assertRaisesRegex(RuntimeError, 'SDK app failed'):
                create_mcp_app(self.db)
        self.assert_closed(runtime)

    def test_composed_factory_failures_release_shared_owner_before_lifespan(self):
        for stage in ('create_http_app', 'create_product_app', 'Starlette'):
            with self.subTest(stage=stage):
                runtime = WorldRuntime(self.db)
                self.addCleanup(runtime.close)
                with patch('agent_world.application.create_mcp_app', return_value=(Mock(), Mock(), runtime)), \
                        patch('agent_world.application.'+stage, side_effect=RuntimeError('composition failed')):
                    with self.assertRaisesRegex(RuntimeError, 'composition failed'):
                        create_application(self.db, world_profile='demo', universe='demo', timers_enabled=False)
                self.assert_closed(runtime)

    def test_world_wrappers_release_inner_owner_when_static_assets_are_missing(self):
        from agent_world import grove_app
        from examples import dnd_app, frontier_app, river_camp_app
        cases = [
            (grove_app, grove_app.create_grove_application, 'existing_world'),
            (dnd_app, dnd_app.create_dnd_application, 'existing_world'),
            (frontier_app, frontier_app.create_frontier_application, 'existing_world'),
            (river_camp_app, river_camp_app.create_camp_application, '_existing_camp'),
        ]
        for module, factory, existing in cases:
            with self.subTest(factory=factory.__name__):
                runtime = WorldRuntime(self.db)
                self.addCleanup(runtime.close)
                inner = Mock()
                inner.state.runtime = runtime
                with patch.object(module, existing, return_value=self.db), \
                        patch.object(module, 'create_application', return_value=inner), \
                        patch.object(module, 'files', return_value=self.db.parent/'missing-wrapper-assets'):
                    with self.assertRaisesRegex(RuntimeError, 'does not exist'):
                        factory(self.db)
                self.assert_closed(runtime)
