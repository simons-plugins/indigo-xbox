"""Shared test doubles for the Xbox plugin unit tests.

``FakeAPI`` records token/JSON calls for :class:`~xb_auth.XboxAuth` and
:mod:`xb_presence` and returns/raises scripted items (a ``dict`` is returned, an
``Exception`` is raised). ``ScriptedTransport`` drives the real
:class:`~xb_api.XboxAPI` HTTP code without a network. No ``indigo``.
"""
from collections import deque
from pathlib import Path

from xb_api import XboxError

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name):
    import json
    with open(FIXTURES / name, encoding="utf-8") as handle:
        return json.load(handle)


class FakeAPI:
    """Records ``post_form`` / ``post_json`` / ``get_json`` calls; returns or
    raises scripted items keyed loosely by call type."""

    def __init__(self):
        self.post_form_calls = []
        self.post_json_calls = []
        self.get_calls = []
        self._post_form = deque()
        self._post_json = deque()
        self._get = deque()

    # queue helpers ----------------------------------------------------------
    def queue_post_form(self, item):
        self._post_form.append(item)
        return self

    def queue_post_json(self, item):
        self._post_json.append(item)
        return self

    def queue_get(self, item):
        self._get.append(item)
        return self

    # api surface ------------------------------------------------------------
    def post_form(self, url, form, headers=None):
        self.post_form_calls.append({"url": url, "form": dict(form), "headers": headers})
        return _pop(self._post_form)

    def post_json(self, url, payload, headers=None):
        self.post_json_calls.append({"url": url, "payload": payload, "headers": headers})
        return _pop(self._post_json)

    def get_json(self, url, headers=None):
        self.get_calls.append({"url": url, "headers": headers})
        return _pop(self._get)


def _pop(queue):
    item = queue.popleft()
    if isinstance(item, Exception):
        raise item
    return item


def oauth_error(key, status=400, retry_after=None, description=None):
    return XboxError(f"HTTP {status} [{key}]", status=status, key=key,
                     description=description, retry_after=retry_after)


def xsts_denied(xerr, status=401):
    return XboxError(f"HTTP {status} [XErr {xerr}]", status=status, xerr=xerr)


# -- Scripted HTTP transport for XboxAPI --------------------------------------

class FakeHTTPResponse:
    """Mimics ``http.client.HTTPResponse`` enough for ``XboxAPI.request``."""

    def __init__(self, status, headers=None, body=b""):
        self.status = status
        self._headers = headers or {}
        self._body = body if isinstance(body, bytes) else body.encode("utf-8")

    def getheaders(self):
        return list(self._headers.items())

    def read(self):
        return self._body


class ScriptedTransport:
    """A ``connection_factory`` that serves queued responses and records requests."""

    def __init__(self):
        self.responses = deque()
        self.requests = []
        self.closed = 0

    def queue(self, status, headers=None, body=b""):
        self.responses.append(FakeHTTPResponse(status, headers, body))
        return self

    def queue_exception(self, exc):
        self.responses.append(exc)
        return self

    def factory(self, host, timeout):  # noqa: ARG002
        return _ScriptedConnection(self, host)


class _ScriptedConnection:
    def __init__(self, transport, host):
        self._transport = transport
        self._host = host

    def request(self, method, url, body=None, headers=None):
        self._transport.requests.append({
            "method": method, "url": url, "body": body,
            "headers": headers, "host": self._host,
        })

    def getresponse(self):
        item = self._transport.responses.popleft()
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self._transport.closed += 1
