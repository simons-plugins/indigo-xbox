"""Stdlib-only HTTP client for the Xbox / MSA REST endpoints.

The Xbox flow spans several hosts (login.microsoftonline.com, user.auth /
xsts.auth.xboxlive.com, peoplehub.xboxlive.com), so every method takes a *full
URL* and the client parses out the host — unlike a single-host client. No
third-party deps: the transport is ``http.client``, injectable
(``connection_factory``) so tests drive it with a fake connection and never
touch the network. This module never imports ``indigo``.
"""
import http.client
import json
import logging
import urllib.parse

FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
JSON_CONTENT_TYPE = "application/json"
DEFAULT_TIMEOUT = 30
DEFAULT_USER_AGENT = "indigo-xbox"


def redact(value):
    """Redact a token to first-4 + last-4 chars so it is safe to log."""
    if not value:
        return value
    text = str(value)
    if len(text) <= 8:
        return "…"
    return f"{text[:4]}…{text[-4:]}"


class XboxError(Exception):
    """An Xbox / MSA API failure.

    Carries the HTTP ``status`` (``None`` for transport-level failures), the
    parsed OAuth error-envelope ``key`` / ``description``, the XSTS ``xerr``
    code (from a 401 body) and any ``retry_after`` (seconds) the server sent.
    """

    def __init__(self, message, status=None, key=None, description=None,
                 retry_after=None, xerr=None):
        super().__init__(message)
        self.status = status
        self.key = key
        self.description = description
        self.retry_after = retry_after
        self.xerr = xerr


class RawResponse:
    """A minimal HTTP response: status, lower-cased headers and body bytes."""

    __slots__ = ("status", "headers", "body")

    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    def header(self, name, default=None):
        return self.headers.get(name.lower(), default)

    def text(self):
        return self.body.decode("utf-8", "replace") if self.body else ""


def _default_connection_factory(host, timeout):
    return http.client.HTTPSConnection(host, timeout=timeout)


class XboxAPI:
    """Low-level multi-host HTTP client. Parses OAuth / XSTS error envelopes."""

    def __init__(self, logger=None, connection_factory=None,
                 user_agent=DEFAULT_USER_AGENT, timeout=DEFAULT_TIMEOUT):
        self._logger = logger or logging.getLogger("xb_api")
        self._connection_factory = connection_factory or _default_connection_factory
        self._user_agent = user_agent
        self._timeout = timeout

    # -- Public helpers ------------------------------------------------------
    def post_form(self, url, form, headers=None):
        """POST an ``x-www-form-urlencoded`` body (MSA token calls) → parsed JSON."""
        body = urllib.parse.urlencode(form)
        req_headers = {"Content-Type": FORM_CONTENT_TYPE}
        if headers:
            req_headers.update(headers)
        raw = self.request("POST", url, headers=req_headers, body=body)
        return self._parse_json(raw, url)

    def post_json(self, url, payload, headers=None):
        """POST a JSON body (Xbox user-token + XSTS calls) → parsed JSON."""
        body = json.dumps(payload)
        req_headers = {"Content-Type": JSON_CONTENT_TYPE}
        if headers:
            req_headers.update(headers)
        raw = self.request("POST", url, headers=req_headers, body=body)
        return self._parse_json(raw, url)

    def get_json(self, url, headers=None):
        """GET → parsed JSON (peoplehub presence)."""
        raw = self.request("GET", url, headers=headers)
        return self._parse_json(raw, url)

    # -- Core request --------------------------------------------------------
    def request(self, method, url, *, headers=None, body=None):
        method = method.upper()
        host, path = _split_url(url)
        conn = self._connection_factory(host, self._timeout)
        req_headers = {"Accept": JSON_CONTENT_TYPE, "User-Agent": self._user_agent}
        if headers:
            req_headers.update(headers)
        try:
            conn.request(method, path, body=body, headers=req_headers)
            resp = conn.getresponse()
            status = resp.status
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            resp_body = resp.read()
        except (OSError, http.client.HTTPException) as exc:
            raise XboxError(f"transport error on {method} {host}: {exc}") from exc
        finally:
            try:
                conn.close()
            except Exception:  # pylint: disable=broad-except
                pass
        raw = RawResponse(status, resp_headers, resp_body)
        if 200 <= status < 300:
            return raw
        raise self._build_error(raw, method, host)

    # -- Internals -----------------------------------------------------------
    def _build_error(self, raw, method, host):
        key = None
        description = None
        xerr = None
        text = raw.text()
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, str):                  # OAuth {"error", "error_description"}
                key = err
                description = data.get("error_description")
            if "XErr" in data:                        # XSTS {"XErr": 2148916238, ...}
                xerr = _int_or_none(data.get("XErr"))
        message = f"HTTP {raw.status} on {method} {host}"
        if key:
            message += f" [{key}]"
        if xerr:
            message += f" [XErr {xerr}]"
        if description:
            message += f": {description}"
        return XboxError(message, status=raw.status, key=key, description=description,
                         retry_after=_parse_retry_after(raw), xerr=xerr)

    def _parse_json(self, raw, url):
        text = raw.text()
        try:
            return json.loads(text)
        except ValueError as exc:
            host, _ = _split_url(url)
            raise XboxError(f"invalid JSON response from {host}: {exc}",
                            status=raw.status) from exc


def _split_url(url):
    parts = urllib.parse.urlsplit(url)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return parts.netloc, path


def _parse_retry_after(raw):
    value = raw.header("retry-after")
    if value is None:
        return None
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return None


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
