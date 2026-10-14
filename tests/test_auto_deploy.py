import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import auto_deploy as deploy


class RecordingHooks(deploy.UIHooks):
    def __init__(self):
        self.statuses = []
        self.lines = []

    def emit(self, line):
        self.lines.append(line)

    def stage_status(self, name, status):
        self.statuses.append((name, status))


class AutoDeployTests(unittest.TestCase):
    def setUp(self):
        self.old_hooks = deploy.HOOKS
        deploy.HOOKS = RecordingHooks()
        deploy.STOP_EVENT.clear()

    def tearDown(self):
        deploy.HOOKS = self.old_hooks
        deploy.STOP_EVENT.clear()

    def test_run_raises_stage_failed_for_nonzero_command(self):
        with self.assertRaises(deploy.StageFailed):
            deploy.run([sys.executable, "-c", "raise SystemExit(7)"])

    def test_read_config_secret_rejects_placeholders(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            path = Path(folder) / ".config.yaml"
            for value in ("你的server.secret值", "null", "none"):
                path.write_text(
                    f"manager-api:\n  secret: {value}\n", encoding="utf-8"
                )
                self.assertIsNone(deploy.read_config_secret(path))
            path.write_text(
                "manager-api:\n  secret: 12345678-abcd\n", encoding="utf-8"
            )
            self.assertEqual(deploy.read_config_secret(path), "12345678-abcd")

    def test_write_manager_config_replaces_only_manager_values(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            root = Path(folder)
            template = root / "template.yaml"
            target = root / "data" / ".config.yaml"
            template.write_text(
                "server:\n  vision_explain: http://example.test\n"
                "manager-api:\n  url: http://old:8002/xiaozhi\n"
                "  secret: placeholder\n",
                encoding="utf-8",
            )
            deploy.write_manager_config(
                target, template, "http://127.0.0.1:8002/xiaozhi", "secret-1"
            )
            text = target.read_text(encoding="utf-8")
            self.assertIn("vision_explain: http://example.test", text)
            self.assertIn("url: http://127.0.0.1:8002/xiaozhi", text)
            self.assertIn("secret: secret-1", text)

    def test_secret_stage_uses_database_without_prompt(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            root = Path(folder)
            server = root / "xiaozhi-server"
            server.mkdir()
            (server / "config_from_api.yaml").write_text(
                "manager-api:\n  url: http://old/xiaozhi\n"
                "  secret: 你的server.secret值\n",
                encoding="utf-8",
            )
            args = SimpleNamespace(reconfigure=False)
            with (
                mock.patch.object(deploy, "XIAOZHI_SERVER", server),
                mock.patch.object(deploy, "read_manager_param", return_value="db-secret"),
                mock.patch.object(
                    deploy, "configure_public_endpoints", return_value=({}, True)
                ),
                mock.patch.object(deploy.HOOKS, "ask_text") as ask_text,
            ):
                deploy.stage_secret(args)
            ask_text.assert_not_called()
            self.assertEqual(
                deploy.read_config_secret(server / "data" / ".config.yaml"),
                "db-secret",
            )

    def test_run_stages_stops_after_first_failure(self):
        called = []

        def fail(_args):
            called.append("fail")
            raise deploy.StageFailed("boom")

        def later(_args):
            called.append("later")

        with mock.patch.dict(
            deploy.STAGE_FUNCS, {"test-fail": fail, "test-later": later}, clear=True
        ):
            ok = deploy.run_stages(
                ["test-fail", "test-later"], SimpleNamespace()
            )
        self.assertFalse(ok)
        self.assertEqual(called, ["fail"])
        self.assertIn(("test-fail", "failed"), deploy.HOOKS.statuses)
        self.assertIn(("test-later", "skipped"), deploy.HOOKS.statuses)

    def test_status_does_not_query_containers_when_docker_is_down(self):
        with (
            mock.patch.object(deploy, "check_docker", return_value=False),
            mock.patch.object(
                deploy, "container_running", side_effect=AssertionError("unexpected")
            ),
            mock.patch.object(deploy, "port_open", return_value=False),
            mock.patch.object(deploy, "pid_alive", return_value=False),
            mock.patch.object(deploy, "load_state", return_value={}),
            mock.patch.object(deploy, "lan_ip", return_value="127.0.0.1"),
        ):
            status = deploy.collect_status()
        self.assertFalse(status["docker_ready"])
        self.assertFalse(any(status["containers"].values()))

    def test_wait_for_port_release_fails_if_port_stays_open(self):
        with (
            mock.patch.object(deploy, "port_open", return_value=True),
            mock.patch.object(deploy.time, "time", side_effect=[0, 0, 2]),
            mock.patch.object(deploy.time, "sleep"),
            self.assertRaises(deploy.StageFailed),
        ):
            deploy.wait_for_port_release(8000, timeout=1)

    def test_public_endpoints_skip_database_write_when_ip_matches(self):
        expected = {
            "server.websocket": "ws://192.0.2.25:8000/xiaozhi/v1/",
            "server.ota": "http://192.0.2.25:8002/xiaozhi/ota/",
        }
        with (
            mock.patch.object(deploy, "lan_ip", return_value="192.0.2.25"),
            mock.patch.object(
                deploy,
                "read_manager_param",
                side_effect=lambda key: expected[key],
            ),
            mock.patch.object(deploy, "write_manager_param") as write_param,
        ):
            values, updated = deploy.configure_public_endpoints()
        self.assertTrue(updated)
        self.assertEqual(values, expected)
        write_param.assert_not_called()

    def test_public_endpoints_replace_stale_ip(self):
        with (
            mock.patch.object(deploy, "lan_ip", return_value="192.0.2.25"),
            mock.patch.object(
                deploy, "read_manager_param", return_value="http://192.168.1.8/"
            ),
            mock.patch.object(
                deploy, "write_manager_param", return_value=True
            ) as write_param,
        ):
            _, updated = deploy.configure_public_endpoints()
        self.assertTrue(updated)
        self.assertEqual(write_param.call_count, 2)


if __name__ == "__main__":
    unittest.main()
