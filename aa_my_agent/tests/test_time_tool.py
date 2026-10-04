import importlib.util
import json
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = PACKAGE_DIR / "tools" / "definitions.py"


def load_definitions_module():
    """隔离外部依赖加载 definitions，不读取真实配置或调用网络。"""
    fake_config = types.ModuleType("aa_my_agent.config")
    fake_config.WORKDIR = PACKAGE_DIR.parent
    fake_config.TAVILY_API_KEY = None
    fake_config.RAG_ALLOW_ON_DEMAND_IMAGE_VERIFY = False

    fake_rag_service = types.ModuleType("aa_my_agent.rag.service")
    fake_rag_service.get_rag_service = lambda: None
    fake_image_verifier = types.ModuleType("aa_my_agent.rag.image_verifier")
    fake_image_verifier.ImageVerificationService = type(
        "ImageVerificationService",
        (),
        {},
    )

    previous_config = sys.modules.get("aa_my_agent.config")
    previous_rag_service = sys.modules.get("aa_my_agent.rag.service")
    previous_image_verifier = sys.modules.get("aa_my_agent.rag.image_verifier")
    sys.modules["aa_my_agent.config"] = fake_config
    sys.modules["aa_my_agent.rag.service"] = fake_rag_service
    sys.modules["aa_my_agent.rag.image_verifier"] = fake_image_verifier

    try:
        name = "aa_my_agent.tools.definitions_time_under_test"
        spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous_config is None:
            sys.modules.pop("aa_my_agent.config", None)
        else:
            sys.modules["aa_my_agent.config"] = previous_config

        if previous_rag_service is None:
            sys.modules.pop("aa_my_agent.rag.service", None)
        else:
            sys.modules["aa_my_agent.rag.service"] = previous_rag_service

        if previous_image_verifier is None:
            sys.modules.pop("aa_my_agent.rag.image_verifier", None)
        else:
            sys.modules["aa_my_agent.rag.image_verifier"] = previous_image_verifier


class CurrentTimeToolTests(unittest.TestCase):
    def setUp(self):
        self.module = load_definitions_module()

    def test_shanghai_time_has_expected_structure_and_offset(self):
        before = int(datetime.now(timezone.utc).timestamp())
        result = json.loads(
            self.module.run_get_current_time("Asia/Shanghai")
        )
        after = int(datetime.now(timezone.utc).timestamp())

        self.assertEqual(result["timezone"], "Asia/Shanghai")
        self.assertEqual(result["utc_offset"], "+0800")
        self.assertRegex(result["date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertRegex(result["time"], r"^\d{2}:\d{2}:\d{2}$")
        self.assertLessEqual(before, result["unix_timestamp"])
        self.assertLessEqual(result["unix_timestamp"], after)

    def test_utc_time_is_supported(self):
        result = json.loads(self.module.run_get_current_time("UTC"))

        self.assertEqual(result["timezone"], "UTC")
        self.assertEqual(result["utc_offset"], "+0000")

    def test_unknown_timezone_returns_error(self):
        result = self.module.run_get_current_time("Mars/Olympus")

        self.assertTrue(result.startswith("Error: Unsupported timezone"))


if __name__ == "__main__":
    unittest.main()
