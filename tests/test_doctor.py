"""witty-agent doctor 自检：打桩环境变量 / which / urlopen，验证判级与退出码。"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import URLError

from witty_agent import doctor

_ENV_KEYS = (
    "WITTY_BASE_URL",
    "WITTY_MODEL_ID",
    "WITTY_API_KEY",
    "OPENAI_API_KEY",
    "WITTY_SEARCH_API_KEY",
    "TAVILY_API_KEY",
    "WITTY_HOME",
    "WITTY_PROMPTS_FILE",
)

_GREEN_ENV = {
    "WITTY_BASE_URL": "https://model.example/v1",
    "WITTY_MODEL_ID": "test-model",
    "WITTY_API_KEY": "sk-test",
    "WITTY_SEARCH_API_KEY": "tvly-test",
}


class _FakeResponse:
    status = 200

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _which_all(name: str) -> str:
    return f"/usr/bin/{name}"


class DoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self._old_env = {key: os.environ.get(key) for key in _ENV_KEYS}
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["WITTY_HOME"] = str(tmp / "home")
        self.scan_root = tmp / "src"
        self.scan_root.mkdir()

    def tearDown(self) -> None:
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def _write_scan_file(self, *keys: str) -> None:
        calls = "\n".join(f'get_prompt("{key}")' for key in keys)
        (self.scan_root / "sample.py").write_text(
            f"from witty_agent.prompts import get_prompt\n{calls}\n",
            encoding="utf-8",
        )

    def _run(self, *, which=_which_all, urlopen=None, skills=({"name": "demo"},)):
        buffer = io.StringIO()
        opener = urlopen or mock.Mock(return_value=_FakeResponse())
        with (
            mock.patch("witty_agent.doctor.urlopen", opener),
            mock.patch("witty_agent.doctor.shutil.which", side_effect=which),
            mock.patch("witty_agent.doctor.list_skills", return_value=list(skills)),
        ):
            code = doctor.run_doctor(scan_root=self.scan_root, stream=buffer)
        return code, buffer.getvalue()

    def test_all_green_exit_zero(self) -> None:
        os.environ.update(_GREEN_ENV)
        self._write_scan_file("harness_system")
        code, out = self._run()
        self.assertEqual(code, 0)
        self.assertNotIn("[FAIL]", out)
        self.assertNotIn("[WARN]", out)
        self.assertEqual(out.count("[OK]"), 8)

    def test_missing_api_key_fails(self) -> None:
        os.environ["WITTY_BASE_URL"] = _GREEN_ENV["WITTY_BASE_URL"]
        os.environ["WITTY_MODEL_ID"] = _GREEN_ENV["WITTY_MODEL_ID"]
        os.environ["WITTY_SEARCH_API_KEY"] = "tvly-test"
        self._write_scan_file("harness_system")
        code, out = self._run()
        self.assertEqual(code, 1)
        self.assertIn("[FAIL]", out)
        self.assertIn("WITTY_API_KEY", out)

    def test_missing_prompt_key_fails(self) -> None:
        os.environ.update(_GREEN_ENV)
        self._write_scan_file("harness_system", "doctor_key_that_never_exists")
        referenced, missing = doctor.audit_prompt_keys(self.scan_root)
        self.assertIn("harness_system", referenced)
        self.assertEqual(missing, {"doctor_key_that_never_exists"})
        code, out = self._run()
        self.assertEqual(code, 1)
        self.assertIn("doctor_key_that_never_exists", out)

    def test_npx_missing_only_warns(self) -> None:
        os.environ.update(_GREEN_ENV)
        self._write_scan_file("harness_system")
        code, out = self._run(which=lambda name: None if name == "npx" else _which_all(name))
        self.assertEqual(code, 0)
        self.assertNotIn("[FAIL]", out)
        self.assertIn("[WARN] Node/npx", out)

    def test_model_unreachable_only_warns(self) -> None:
        os.environ.update(_GREEN_ENV)
        self._write_scan_file("harness_system")
        code, out = self._run(urlopen=mock.Mock(side_effect=URLError("connection refused")))
        self.assertEqual(code, 0)
        self.assertNotIn("[FAIL]", out)
        self.assertEqual(out.count("[WARN]"), 1)

    def test_search_key_missing_warns(self) -> None:
        os.environ.update(_GREEN_ENV)
        os.environ.pop("WITTY_SEARCH_API_KEY", None)
        self._write_scan_file("harness_system")
        code, out = self._run()
        self.assertEqual(code, 0)
        self.assertIn("[WARN]", out)
        self.assertIn("WITTY_SEARCH_API_KEY", out)


if __name__ == "__main__":
    unittest.main()
