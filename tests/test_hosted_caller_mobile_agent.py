from pathlib import Path


EXAMPLE_ROOT = Path(__file__).resolve().parents[1] / "examples" / "hosted_caller_mobile_agent"


def test_hosted_mobile_example_uses_real_sdk_and_run_scoped_mobile_api():
    source = (EXAMPLE_ROOT / "agent.py").read_text(encoding="utf-8")
    dockerfile = (EXAMPLE_ROOT / "Dockerfile").read_text(encoding="utf-8")

    compile(source, str(EXAMPLE_ROOT / "agent.py"), "exec")
    assert "NexusMCPServer" in source
    assert "CurrentNexusMCP" in source
    assert "chat=True" in source
    assert "mobile_scopes=MOBILE_SCOPES" in source
    assert "await nexus.mobile.capture_screen()" in source
    assert "await nexus.mobile.type_text(marker" in source
    assert "await nexus.browser.frame(" in source
    assert "await nexus.chat.say(" in source
    assert "COPY dist/*.whl" in dockerfile
    assert "0.36.0" not in dockerfile
