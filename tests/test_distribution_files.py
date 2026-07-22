import configparser
from pathlib import Path
import shlex
import tomllib


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _load_systemd_unit() -> configparser.ConfigParser:
    unit = configparser.ConfigParser(
        interpolation=None,
        strict=True,
        empty_lines_in_values=False,
    )
    unit.optionxform = str
    unit.read_string(_read("deploy/mail-manager.service"))
    return unit


def _parse_caddyfile() -> dict:
    root = {"header": None, "lines": [], "blocks": []}
    stack = [root]
    for line_number, raw_line in enumerate(
        _read("deploy/Caddyfile").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line == "}":
            assert len(stack) > 1, f"unexpected closing brace on line {line_number}"
            stack.pop()
            continue
        if line.endswith("{"):
            header = line[:-1].strip()
            assert header, f"missing block header on line {line_number}"
            block = {"header": header, "lines": [], "blocks": []}
            stack[-1]["blocks"].append(block)
            stack.append(block)
            continue
        stack[-1]["lines"].append(line)
    assert stack == [root], "Caddyfile contains an unclosed block"
    return root


def _only_block(parent: dict, header: str) -> dict:
    matches = [block for block in parent["blocks"] if block["header"] == header]
    assert len(matches) == 1
    return matches[0]


def test_license_is_complete_gpl_v3_only_text():
    license_text = _read("LICENSE")

    assert "GNU GENERAL PUBLIC LICENSE" in license_text
    assert "Version 3, 29 June 2007" in license_text
    assert "17. Interpretation of Sections 15 and 16." in license_text
    assert "END OF TERMS AND CONDITIONS" in license_text
    assert len(license_text) > 30_000


def test_package_metadata_uses_pep_639_without_legacy_license_classifier():
    project = tomllib.loads(_read("pyproject.toml"))["project"]

    assert project["readme"] == "README.md"
    assert project["license"] == "GPL-3.0-only"
    assert all(
        not classifier.startswith("License ::")
        for classifier in project["classifiers"]
    )


def test_readme_documents_safe_install_operations_and_architecture():
    readme = _read("README.md")

    for section in (
        "## 功能",
        "## 架构",
        "## 快速开始",
        "## 测试",
        "## 生产部署",
        "## 备份",
        "## 升级",
    ):
        assert section in readme
    assert "ACCESS_PASSWORD" in readme
    assert "SESSION_SECRET" in readme
    assert "Fernet.generate_key" in readme
    assert "--no-build-isolation --no-deps -e ." in readme
    assert "flask --app 'mail_manager:create_app()' init-db" in readme
    assert "TEST_DATABASE_URL" in readme
    assert "127.0.0.1:5000" in readme
    assert "禁止" in readme and "公网" in readme and "Gunicorn" in readme
    assert "install -m 600 deploy/mail-manager.env.example .env" in readme
    assert (
        "install -o root -g root -m 600 deploy/mail-manager.env.example "
        "/etc/mail-manager/mail-manager.env"
    ) in readme
    assert "权限设为 `0600`" in readme
    assert "0640" not in readme


def test_systemd_unit_is_non_root_loopback_only_and_hardened():
    unit = _load_systemd_unit()
    assert unit.sections() == ["Unit", "Service", "Install"]
    service = unit["Service"]

    assert service["User"] == "mail-manager"
    assert service["Group"] == "mail-manager"
    assert service["EnvironmentFile"] == "/etc/mail-manager/mail-manager.env"
    init_command = shlex.split(service["ExecStartPre"])
    assert init_command == [
        "/opt/mail-manager/.venv/bin/flask",
        "--app",
        "mail_manager:create_app()",
        "init-db",
    ]
    start_command = shlex.split(service["ExecStart"])
    bind_index = start_command.index("--bind")
    assert start_command[bind_index + 1] == "127.0.0.1:5000"
    assert start_command[-1] == "mail_manager:create_app()"

    expected_hardening = {
        "NoNewPrivileges": "true",
        "PrivateTmp": "true",
        "PrivateDevices": "true",
        "ProtectSystem": "strict",
        "ProtectHome": "true",
        "ProtectKernelTunables": "true",
        "ProtectKernelModules": "true",
        "ProtectControlGroups": "true",
        "CapabilityBoundingSet": "",
        "RestrictAddressFamilies": "AF_UNIX AF_INET AF_INET6",
        "LimitNOFILE": "4096",
        "TasksMax": "128",
        "MemoryMax": "1G",
    }
    for name, value in expected_hardening.items():
        assert service[name] == value
    serialized_values = "\n".join(service.values())
    for secret in (
        "ACCESS_PASSWORD",
        "SESSION_SECRET",
        "CREDENTIALS_ENCRYPTION_KEY",
    ):
        assert secret not in serialized_values


def test_caddy_terminates_tls_limits_requests_and_sets_security_headers():
    parsed = _parse_caddyfile()
    site = _only_block(parsed, "{$MAIL_MANAGER_DOMAIN:mail.example.com}")
    request_body = _only_block(site, "request_body")
    assert request_body["lines"] == ["max_size 2MiB"]

    proxy = _only_block(site, "reverse_proxy 127.0.0.1:5000")
    transport = _only_block(proxy, "transport http")
    assert transport["lines"] == [
        "dial_timeout 5s",
        "response_header_timeout 30s",
    ]

    headers = _only_block(site, "header")["lines"]
    for header in (
        "Strict-Transport-Security",
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Referrer-Policy",
        "Permissions-Policy",
    ):
        assert any(line.startswith(f"{header} ") for line in headers)


def test_environment_example_contains_no_secret_defaults():
    environment = _read("deploy/mail-manager.env.example")
    settings = {}
    for line in environment.splitlines():
        if line and not line.startswith("#"):
            name, value = line.split("=", 1)
            assert name not in settings
            settings[name] = value

    for name in (
        "ACCESS_PASSWORD",
        "SESSION_SECRET",
        "CREDENTIALS_ENCRYPTION_KEY",
    ):
        assert settings[name] == ""
    assert "mode 0600" in environment
    assert "0640" not in environment
    assert "secrets.token_urlsafe" in environment
    assert "Fernet.generate_key" in environment
    assert settings["TRUST_PROXY"] == "true"
    assert settings["TRUSTED_PROXY_CIDRS"] == "127.0.0.1/32,::1/128"


def test_security_and_contribution_policies_are_present():
    security = _read("SECURITY.md")
    contributing = _read("CONTRIBUTING.md")

    assert "Private vulnerability reporting" in security
    assert "公开 Issue" in security
    assert "红-绿-重构" in contributing
    assert "GPL-3.0-only" in contributing
    assert "秘密" in contributing
