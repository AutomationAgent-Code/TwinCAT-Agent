import base64
import hashlib
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace
from unittest.mock import patch
import zlib

import pytest

from scripts.bundle_tool_scripts import bundle
from tc_template import _packaged_scripts as resources


def test_bundled_fallbacks_match_source_and_are_colocated(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    bundle(repo, [tmp_path])
    scripts = runpy.run_path(str(tmp_path / '_script_bundle.py'))['SCRIPTS']
    assert set(scripts) == {'TcCom.ps1', 'TcHmiBinding.ps1', 'TcHmiServer.ps1', 'TcHmiItems.ps1', 'TcHmiProject.ps1', 'TcIoConfiguration.ps1'}
    with patch.dict(sys.modules, {'tc_template._script_bundle': SimpleNamespace(SCRIPTS=scripts)}), \
         patch.object(resources, '_directory', None):
        try:
            main = resources.script_path('TcCom.ps1')
            assert main.read_bytes() == (repo / 'tc_template' / 'TcCom.ps1').read_bytes()
            assert resources.script_path('TcHmiServer.ps1').parent == main.parent
            assert resources.script_path('TcHmiProject.ps1').read_bytes() == (repo / 'tc_template' / 'TcHmiProject.ps1').read_bytes()
            assert resources.script_path('TcCom.ps1') == main
            with pytest.raises(FileNotFoundError):
                resources.script_path('../unexpected.ps1')
        finally:
            if resources._directory:
                resources._directory.cleanup()


def test_corrupt_resource_rejected_without_publishing_cache():
    scripts = {'TcCom.ps1': ('wrong-hash', base64.b85encode(zlib.compress(b'test')).decode())}
    with patch.dict(sys.modules, {'tc_template._script_bundle': SimpleNamespace(SCRIPTS=scripts)}), \
         patch.object(resources, '_directory', None), pytest.raises(ValueError, match='integrity'):
        resources.script_path('TcCom.ps1')
    assert resources._directory is None
