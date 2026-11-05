import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from aiohttp.test_utils import TestClient, TestServer
from backend.main import create_app


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def test_page_assets_and_state_without_hardware(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            with patch("backend.utils.DATA_DIR", folder), patch("backend.app.DATA_DIR", folder):
                app = create_app()
                client = TestClient(TestServer(app))
                await client.start_server()
                try:
                    for path in [
                        "/",
                        "/static/js/app.js",
                        "/static/js/vr.js",
                        "/static/js/quest_camera.js",
                        "/static/css/style.css",
                        "/api/state",
                    ]:
                        response = await client.get(path)
                        self.assertEqual(response.status, 200, path)
                        await response.read()
                    self.assertIsNone(app["state"].motor.config["grippers"]["18"]["opened"])
                finally:
                    await client.close()
