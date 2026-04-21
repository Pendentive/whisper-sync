"""Tests for whisper_sync.crash_diagnostics helpers."""

import unittest


class ExtractFaultingModuleTests(unittest.TestCase):
    def test_extracts_tcl_module(self):
        from whisper_sync.crash_diagnostics import _extract_faulting_module
        msg = (
            "Faulting application name: python.exe, version: 3.13.0.1013 "
            "| Faulting module name: tcl86t.dll, version: 8.6.13.0 "
            "| Exception code: 0xc0000005"
        )
        self.assertEqual(_extract_faulting_module(msg), "tcl86t.dll")

    def test_extracts_torch_module(self):
        from whisper_sync.crash_diagnostics import _extract_faulting_module
        msg = "... Faulting module name: torch_cuda.dll ..."
        self.assertEqual(_extract_faulting_module(msg), "torch_cuda.dll")

    def test_returns_none_when_absent(self):
        from whisper_sync.crash_diagnostics import _extract_faulting_module
        self.assertIsNone(_extract_faulting_module("no module info here"))


if __name__ == "__main__":
    unittest.main()
