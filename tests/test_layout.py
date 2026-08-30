from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from witty_agent.layout import (
    DEFAULT_AGENT_ID,
    agent_state_dir,
    assert_id,
    data_root,
    project_config_path,
    snapshots_dir,
)


class LayoutTests(unittest.TestCase):
    def test_default_root(self) -> None:
        os.environ.pop("WITTY_HOME", None)
        self.assertEqual(data_root(), Path.home() / ".witty" / "data")

    def test_witty_home_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["WITTY_HOME"] = tmp
            try:
                root = data_root()
                self.assertEqual(root, Path(tmp).resolve())
                state = agent_state_dir("grid-base", DEFAULT_AGENT_ID, root=root)
                self.assertEqual(state, root / "grid-base" / "agents" / "default_agent" / "agent_state")
                self.assertEqual(
                    project_config_path("grid-base", root=root),
                    root / "grid-base" / ".project_config.toml",
                )
                self.assertTrue(str(snapshots_dir("grid-base", root=root)).endswith("snapshots"))
            finally:
                os.environ.pop("WITTY_HOME", None)

    def test_reject_path_id(self) -> None:
        with self.assertRaises(ValueError):
            assert_id("project_id", "../etc")
        with self.assertRaises(ValueError):
            assert_id("agent_id", "Default_Agent")


if __name__ == "__main__":
    unittest.main()
