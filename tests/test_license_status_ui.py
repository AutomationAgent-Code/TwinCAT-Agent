from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class LicenseStatusUiRegressionTests(unittest.TestCase):
    def test_authorized_header_keeps_version_and_expiry_status(self):
        html = (ROOT / "tc_agent" / "static" / "index.html").read_text(encoding="utf-8")
        for marker in ('id="versionTag"', 'id="licenseSummary"',
                       'id="licenseGateVersion"', "renderLicenseSummary()",
                       's.customer', 's.license_id', 's.expires_at'):
            self.assertIn(marker, html)

    def test_backend_attaches_application_version_to_license_status(self):
        source = (ROOT / "tc_agent" / "backend.py").read_text(encoding="utf-8")
        self.assertIn('APP_VERSION = (PROJECT_DIR / "VERSION")', source)
        self.assertIn('result["app_version"] = APP_VERSION', source)

    def test_portable_package_includes_version_file(self):
        source = (ROOT / "scripts" / "build_portable.ps1").read_text(encoding="utf-8-sig")
        self.assertIn('(Join-Path $Repo "VERSION") (Join-Path $App "VERSION")', source)


if __name__ == "__main__":
    unittest.main()
