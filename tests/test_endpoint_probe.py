"""Status probe never mistakes authentication or WAF rejection for success."""
import importlib.util
from pathlib import Path
from email.message import Message
import pytest

spec = importlib.util.spec_from_file_location("check_endpoint", Path(__file__).parents[1] / "scripts/check_endpoint.py")
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def headers(location="https://team.cloudflareaccess.com/cdn-cgi/access/login/hippo.example.com?token=not-logged"):
    h = Message()
    h["Location"] = location
    h["WWW-Authenticate"] = "Cloudflare-Access"
    return h


def test_expected_login_is_auth_required_not_business_success():
    assert module.classify(302, headers(), "team.cloudflareaccess.com") == "AUTH_REQUIRED"


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_error_never_classified_healthy(status):
    assert module.classify(status, headers(), "team.cloudflareaccess.com") == "UNEXPECTED_HTTP"


def test_redirect_to_other_host_not_trusted():
    assert module.classify(302, headers("https://other.invalid/cdn-cgi/access/login/a"), "team.cloudflareaccess.com") == "UNEXPECTED_HTTP"


def test_redirect_handler_does_not_follow():
    assert module.NoRedirect().redirect_request(None, None, 302, "", headers(), "https://example.invalid") is None


def test_probe_rejects_credentials_or_query_before_network():
    for url in ["https://name:password@example.invalid/health", "https://example.invalid/health?token=abc", "file:///etc/passwd"]:
        with pytest.raises(ValueError):
            module.probe(url)
