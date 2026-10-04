import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aa_my_agent import telemetry


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        telemetry._session_path = None
        telemetry.set_turn_id(None)

    def tearDown(self):
        telemetry._session_path = None
        telemetry.set_turn_id(None)
        self.temp_dir.cleanup()

    def test_events_are_written_as_jsonl_with_turn_id(self):
        path = telemetry.start_session_log(Path(self.temp_dir.name))
        telemetry.set_turn_id("turn-test")
        telemetry.emit("TEST", "Event", status="ok", count=2)

        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(len(records), 2)
        self.assertEqual(records[1]["turn_id"], "turn-test")
        self.assertEqual(records[1]["status"], "ok")
        self.assertEqual(records[1]["count"], 2)

    def test_response_usage_supports_objects_and_dicts(self):
        object_response = SimpleNamespace(
            usage=SimpleNamespace(input_tokens=10, output_tokens=3)
        )
        dict_response = SimpleNamespace(
            usage={"input_tokens": 8, "cache_read_input_tokens": 4}
        )

        self.assertEqual(telemetry.response_usage(object_response)["input"], 10)
        self.assertEqual(telemetry.response_usage(object_response)["output"], 3)
        self.assertEqual(telemetry.response_usage(dict_response)["input"], 8)
        self.assertEqual(telemetry.response_usage(dict_response)["cache_read"], 4)

    def test_turn_metrics_aggregate_components(self):
        telemetry.start_turn("turn-metrics")
        telemetry.record_model_response(
            "memory_select",
            SimpleNamespace(usage={"input_tokens": 8, "output_tokens": 1}),
        )
        telemetry.record_model_response(
            "main",
            SimpleNamespace(usage={"input_tokens": 20, "output_tokens": 5}),
        )

        metrics = telemetry.turn_model_metrics()
        self.assertEqual(metrics["total"]["calls"], 2)
        self.assertEqual(metrics["total"]["input"], 28)
        self.assertEqual(metrics["components"]["main"]["output"], 5)


if __name__ == "__main__":
    unittest.main()
