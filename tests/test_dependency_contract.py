"""Dependency contracts are checked against installed wheel metadata."""
from importlib.metadata import metadata
import unittest
from packaging.requirements import Requirement


class DependencyContractTests(unittest.TestCase):
    def setUp(self):
        self.requires = [Requirement(raw) for raw in metadata("nexilume").get_all("Requires-Dist", [])]

    def dependencies(self, extra, python="3.10"):
        return {r.name: r for r in self.requires
                if r.marker is None or r.marker.evaluate({"extra": extra, "python_version": python})}

    def test_core_stays_dependency_free(self):
        self.assertEqual(self.dependencies(""), {})

    def test_computer_toml_dependency_is_version_scoped(self):
        for version in ("3.9", "3.10"):
            self.assertIn("tomli", self.dependencies("computer", version))
        for version in ("3.11", "3.12", "3.14"):
            self.assertNotIn("tomli", self.dependencies("computer", version))

    def test_direct_integration_imports_have_explicit_dependencies(self):
        for extra in ("fastmcp", "fastmcp-tasks"):
            self.assertTrue({"fastmcp", "mcp", "pydantic", "jsonschema", "uvicorn"}
                            <= self.dependencies(extra).keys())
        self.assertTrue({"a2a-sdk", "httpx", "protobuf", "pydantic"} <= self.dependencies("a2a").keys())

    def test_task_extra_uses_official_fastmcp_extra(self):
        requirements = self.dependencies("fastmcp-tasks")
        self.assertNotIn("fastmcp-tasks", requirements)
        self.assertIn("tasks", requirements["fastmcp"].extras)

    def test_windows_dependency_is_platform_scoped(self):
        windows = next(r for r in self.requires if r.name == "pywin32")
        self.assertTrue(windows.marker.evaluate({"extra": "windows", "platform_system": "Windows"}))
        self.assertFalse(windows.marker.evaluate({"extra": "windows", "platform_system": "Linux"}))

    def test_browser_does_not_select_legacy_python39_builds(self):
        playwright = self.dependencies("browser")["playwright"]
        self.assertFalse(playwright.specifier.contains("1.60.0"))
        self.assertFalse(playwright.specifier.contains("1.61.0"))
        self.assertTrue(playwright.specifier.contains("1.63.0"))
