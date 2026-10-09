"""Regression tests for bugs fixed during the systematic audit.

Each test pins a real defect that was found and corrected:
- shell injection via user-controlled strings
- fabricated installers / providers / model names
- weak P2P network classification and path confinement
- placeholder discovery server treated as real
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault("JADE_SKIP_STARTUP", "1")


def test_system_find_no_shell_injection(tmp_path):
    from tools import search
    marker = tmp_path / "pwned"
    # If `name` were interpolated into shell=True, this would create the file.
    result = search.system_find(f"x\"; touch {marker}; echo \"", root=str(tmp_path))
    assert isinstance(result, str)
    assert not marker.exists()


def test_copilot_uses_argv_not_shell():
    import subprocess
    from unittest.mock import patch
    from tools import copilot
    with patch.object(subprocess, "run") as mock_run:
        mock_run.return_value.stdout = "ok"
        mock_run.return_value.stderr = ""
        copilot.copilot_suggest('"; touch /tmp/pwned; echo "')
        cmd = mock_run.call_args[0][0]
        assert isinstance(cmd, list), "copilot_suggest must not use shell=True"
        assert mock_run.call_args[1].get("shell") is not True
    with patch.object(subprocess, "run") as mock_run:
        mock_run.return_value.stdout = "ok"
        mock_run.return_value.stderr = ""
        copilot.copilot_explain('"; touch /tmp/pwned; echo "')
        cmd = mock_run.call_args[0][0]
        assert isinstance(cmd, list), "copilot_explain must not use shell=True"


def test_installer_quotes_package_names():
    from unittest.mock import patch
    from tools import installer
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return {"ok": True}

    with patch.object(installer, "run", fake_run):
        installer.brew_install("foo; rm -rf ~")
        assert "'foo; rm -rf ~'" in captured["cmd"] or '"foo; rm -rf ~"' in captured["cmd"], \
            f"package name not quoted: {captured['cmd']}"
        installer.git_install("https://example.com/r.git; evil", "dest; evil")
        # Both arguments must appear only inside shlex quotes.
        assert "'https://example.com/r.git; evil'" in captured["cmd"]
        assert "'dest; evil'" in captured["cmd"]


def test_agent_manager_uses_real_distribution_channels():
    from core import agent_manager
    for name, cmds in agent_manager.AGENT_REGISTRY.items():
        install = cmds["install"]
        # None of these tools are distributed via PyPI under invented names.
        assert not install.startswith("pip install "), f"{name}: fabricated pip installer"
        assert any(k in install for k in ("npm ", "curl ", "gh extension ")), \
            f"{name}: unknown install channel: {install}"


def test_known_providers_has_no_fabrications():
    from core.services import KNOWN_PROVIDERS
    fabricated = {"midjourney", "sora", "kling", "heygen", "veo", "flux", "whisper",
                  "polly", "wolfram", "laguna", "essential", "mindsdb", "kwaipilot",
                  "replit", "athene", "reflection", "z_ai", "smollm", "minicpm"}
    assert not (fabricated & set(KNOWN_PROVIDERS)), \
        f"fabricated providers advertised: {fabricated & set(KNOWN_PROVIDERS)}"
    # Every remaining provider must have validation or key handling.
    from core.services import validate_provider_connection
    for p in ("openai", "anthropic", "gemini", "ollama", "mistral", "deepseek",
              "groq", "together", "cohere", "perplexity"):
        assert p in KNOWN_PROVIDERS


def test_p2p_private_classification():
    from core.p2p import _is_private_peer
    assert _is_private_peer("192.168.1.5") is True
    assert _is_private_peer("10.0.0.9") is True
    assert _is_private_peer("127.0.0.1") is True
    # The old startswith("172.16.") check missed most of 172.16/12.
    assert _is_private_peer("172.20.5.4") is True
    assert _is_private_peer("172.31.255.1") is True
    assert _is_private_peer("8.8.8.8") is False
    assert _is_private_peer("not-an-ip") is False


def test_p2p_path_confinement():
    from core.p2p import _is_path_confined, WORKSPACE_ROOT
    assert _is_path_confined("/etc/passwd") is None
    assert _is_path_confined(str(WORKSPACE_ROOT / ".." / "secret.txt")) is None
    assert _is_path_confined("~/.ssh/id_rsa") is None
    inside = _is_path_confined(str(WORKSPACE_ROOT / "cli.py"))
    assert inside is not None and inside.name == "cli.py"


def test_p2p_token_compare_is_constant_time():
    import inspect
    from core import p2p
    src = inspect.getsource(p2p.JadeP2PHandler.do_POST)
    assert "compare_digest" in src


def test_global_p2p_placeholder_server_is_unset():
    from core.global_p2p import _server_url
    assert _server_url({}) is None  # example.com placeholder must not be used
    assert _server_url({"discovery_server_url": "https://example.com/x"}) is None
    assert _server_url({"discovery_server_url": "https://discovery.internal:8443"}) == \
        "https://discovery.internal:8443"


def test_brain_prompt_has_no_refusal_bypass():
    import ast
    tree = ast.parse(open("core/brain.py").read())
    prompt = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "SYSTEM_PROMPT":
                    prompt = ast.get_source_segment(open("core/brain.py").read(), node)
    assert prompt is not None
    lowered = prompt.lower()
    assert "ignore it" not in lowered
    assert "zero safety checks" not in lowered
    assert "morality filters" not in lowered


def test_prompt_presets_have_no_refusal_bypass():
    from core.prompts import DEFAULT_PROMPTS
    assert "unrestricted" not in DEFAULT_PROMPTS
    assert "nave_sovereign" not in DEFAULT_PROMPTS
    for name, text in DEFAULT_PROMPTS.items():
        lowered = text.lower()
        assert "never refuse" not in lowered, name
        assert "zero restrictions" not in lowered, name
        assert "unconditional disclosure" not in lowered, name
    assert "never ask for permission" not in DEFAULT_PROMPTS["default"].lower()
