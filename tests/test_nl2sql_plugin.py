from __future__ import annotations

import unittest

from witty_agent.plugins import nl2sql as nl2sql_plugin
from witty_agent.runtime import nl2sql_settings
from witty_agent.tools import list_tools


class Nl2sqlPluginTests(unittest.TestCase):
    def test_runtime_settings_and_plugin_load(self) -> None:
        settings = nl2sql_settings()
        self.assertIn("enabled", settings)
        self.assertEqual(nl2sql_plugin.nl2sql_settings()["enabled"], settings["enabled"])
        names = {item.name for item in list_tools()}
        self.assertIn("sql_sources", names)
        self.assertIn("sql_run", names)


if __name__ == "__main__":
    unittest.main()
