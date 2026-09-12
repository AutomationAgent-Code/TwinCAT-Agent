import io
import json
import unittest
from unittest.mock import patch

from tc_template import _native_worker as worker


class NativeWorkerEncodingTests(unittest.TestCase):
    def test_non_ascii_result_is_emitted_as_ascii_json(self):
        stdout = io.StringIO()
        with patch.object(worker.sys, "stdin", io.StringIO('{"command":"x"}')), \
             patch.object(worker.sys, "stdout", stdout), \
             patch.object(worker, "_execute", return_value={"text": "轴↔PLC"}):
            self.assertEqual(0, worker.main())
        encoded = stdout.getvalue().encode("ascii")
        self.assertEqual("轴↔PLC", json.loads(encoded)["data"]["text"])


if __name__ == "__main__":
    unittest.main()
