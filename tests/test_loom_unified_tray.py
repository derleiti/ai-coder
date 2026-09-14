from pathlib import Path
import unittest


class LoomUnifiedTrayContractTests(unittest.TestCase):
    def test_aicoder_tray_is_suppressed_only_in_unified_mode(self):
        source=(Path(__file__).resolve().parents[1]/"aicoder/gui/app.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.get("AILINUX_LOOM_UNIFIED") == "1"',source)
        self.assertIn('"--loom-unified" in sys.argv',source)
        self.assertIn('if not unified_mode:\n        tray.show()',source)
        self.assertIn('window.tray = None if unified_mode else tray',source)
