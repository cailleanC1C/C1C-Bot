from modules.ops.startup_progress import STARTUP_PHASES, deployment_identity, render_startup_failed, render_startup_progress, render_startup_ready


def test_deployment_identity_prefers_render_commit(monkeypatch) -> None:
    monkeypatch.setenv("RENDER_GIT_COMMIT", "0123456789abcdef")
    assert deployment_identity(version="2.4.1", env="prod") == "commit=0123456 • version=2.4.1 • env=prod"


def test_startup_progress_renders_every_real_phase() -> None:
    states = {phase: "⏳" for phase in STARTUP_PHASES}
    states["Core initialization"] = "✅"
    states["Schedulers"] = "🔄"
    message = render_startup_progress(identity="commit=abc1234 • version=1 • env=prod", states=states)
    assert "🚀 Woadkeeper starting…" in message
    assert "✅ Core initialization" in message
    assert "🔄 Schedulers" in message
    assert "⏳ Watchdog & keepalive" in message
    assert "⏳ Startup refresh" in message


def test_ready_and_failed_messages_keep_deployment_identity() -> None:
    identity = "commit=abc1234 • version=1 • env=prod"
    ready = render_startup_ready(identity=identity, duration_s=18.74)
    failed = render_startup_failed(identity=identity, phase="Core initialization")
    assert ready == "✅ Woadkeeper ready\n" + identity + "\nstartup=18.7s"
    assert failed == "❌ Woadkeeper startup failed\n" + identity + "\nphase=Core initialization"
