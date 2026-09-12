import unittest
from unittest.mock import patch

from tc_template import tc_platform as platform


class FakeInstance:
    AdsPort = 851

    def ProduceXml(self, *_args):
        return """<TreeItem>
          <TcModuleInstance><Module><ParameterValues>
            <Value><Name>Project Name</Name><String>PlcProject</String></Value>
            <Value><Name>Application Timestamp</Name><Value>123456</Value></Value>
          </ParameterValues></Module></TcModuleInstance>
        </TreeItem>"""


class DeployVerificationTests(unittest.TestCase):
    def test_local_identity_is_read_from_built_plc_instance(self):
        runtime = {
            "name": "PLC Instance",
            "project_name": "PlcProject",
            "ads_port": 851,
            "instance": FakeInstance(),
        }
        with patch.object(platform, "_list_plc_runtimes", return_value=[runtime]):
            identities = platform._local_plc_identities(object())
        self.assertEqual(identities[851]["application_timestamp"], 123456)
        self.assertEqual(identities[851]["project_name"], "PlcProject")

    def test_identity_mismatch_rejects_stale_running_application(self):
        expected = {"application_timestamp": 123, "project_name": "NewProject"}
        actual = {"application_timestamp": 122, "project_name": "OldProject"}
        with patch.object(platform, "_read_target_plc_identity", return_value=actual):
            result = platform._verify_target_plc_identity("1.2.3.4.1.1", 851, expected)
        self.assertFalse(result["verified"])
        self.assertFalse(result["timestamp_matches"])

    def test_online_does_not_accept_run_state_when_login_failed(self):
        class Sysman:
            def GetTargetNetId(self):
                return "1.2.3.4.1.1"

        runtimes = [{
            "name": "PLC Instance",
            "ads_port": 851,
            "port_source": "property:AdsPort",
        }]
        cycle = {
            "login": {"status": "failed"},
            "start": {"status": "ok"},
        }
        with patch.object(platform, "_sysman", return_value=Sysman()), \
             patch.object(platform, "_list_plc_runtimes", return_value=runtimes), \
             patch.object(platform, "full_online_cycle", return_value=cycle), \
             patch.object(platform, "_poll_ads_state", return_value={"verified": True, "state": 5}), \
             patch.object(platform.time, "sleep"):
            result = platform._online_loop(max_retries=1)
        self.assertFalse(result["verified"])
        self.assertFalse(result["commands_verified"])
        self.assertEqual(result["plcs"][0]["command_error"], "Login or Start command failed")

    def test_online_rejects_run_state_with_old_application_identity(self):
        class Sysman:
            def GetTargetNetId(self):
                return "1.2.3.4.1.1"

        runtimes = [{
            "name": "PLC Instance",
            "ads_port": 851,
            "port_source": "property:AdsPort",
        }]
        cycle = {"status": "verified", "verified": True}
        with patch.object(platform, "_sysman", return_value=Sysman()), \
             patch.object(platform, "_list_plc_runtimes", return_value=runtimes), \
             patch.object(platform, "full_online_cycle", return_value=cycle), \
             patch.object(platform, "_poll_ads_state", return_value={"verified": True, "state": 5}), \
             patch.object(platform, "_verify_target_plc_identity", return_value={"verified": False}), \
             patch.object(platform.time, "sleep"):
            result = platform._online_loop(
                max_retries=1,
                expected_identities={851: {"application_timestamp": 123}},
            )
        self.assertFalse(result["verified"])
        self.assertFalse(result["plcs"][0]["identity"]["verified"])


if __name__ == "__main__":
    unittest.main()
