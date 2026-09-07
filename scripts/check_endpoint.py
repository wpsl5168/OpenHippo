"""Safe health probe: report an Access login redirect, never follow it.

Usage: python scripts/check_endpoint.py https://hippo.example.com/health \
    --access-host team.cloudflareaccess.com
AUTH_REQUIRED verifies only the authentication entrance, not logged-in access.
"""
import argparse
import json
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def classify(status, headers, expected_access_host):
    location = urllib.parse.urlsplit(headers.get("Location", ""))
    gate = (status in (302, 303, 307) and expected_access_host
            and location.scheme == "https" and location.hostname == expected_access_host
            and location.path.startswith("/cdn-cgi/access/login/")
            and "cloudflare-access" in headers.get("WWW-Authenticate", "").lower())
    if gate:
        return "AUTH_REQUIRED"
    return "HTTP_OK" if status == 200 else "UNEXPECTED_HTTP"


def probe(url, expected_access_host=None):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Provide an HTTP(S) URL without credentials, query or fragment")
    request = urllib.request.Request(url, method="GET")
    opener = urllib.request.build_opener(NoRedirect())
    try:
        response = opener.open(request, timeout=25)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        status, headers = response.code, response.headers
        location = urllib.parse.urlsplit(headers.get("Location", ""))
        return {"url": url, "status": status, "result": classify(status, headers, expected_access_host),
                "redirects_followed": 0, "location_host": location.hostname,
                "location_path": location.path, "cf_ray": headers.get("CF-Ray"),
                "business_access_verified": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--access-host")
    args = parser.parse_args()
    result = probe(args.url, args.access_host)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["result"] in ("HTTP_OK", "AUTH_REQUIRED") else 2)
