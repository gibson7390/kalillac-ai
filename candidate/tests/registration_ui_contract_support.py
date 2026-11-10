
"""Extend the existing fake server's contract, without changing legacy UI tests."""
CONTRACT_PATH = "/api/account/registration-contract"

def install_contract(monkeypatch, api_class, mode="legacy-v1"):
    original = api_class.handle
    def handle(self, method, path, headers, raw):
        if method != "GET" or path != CONTRACT_PATH:
            return original(self, method, path, headers, raw)
        with self.lock:
            self.calls.append({"method": method, "path": path, "body": None})
            waiting = self.holds.get((method, path))
            gate = waiting.pop(0) if waiting else None
        if gate:
            gate.arrived.set()
            assert gate.released.wait(10), "contract request never released"
            if gate.response: return gate.response
        with self.lock:
            if not self.enabled: return 404, {"detail": "Not Found"}, {}
            self.last_contract_nonce = headers.get("X-Kalillac-Registration-Nonce")
            queued = self.overrides.get((method, path))
            if queued:
                status, body, extra = queued.pop(0)
                if isinstance(body, dict) and body.get("nonce") == "$request_nonce":
                    body = dict(body, nonce=self.last_contract_nonce)
                return status, body, extra
            return 200, {"contract": getattr(self, "registration_contract", mode),
                         "nonce": headers.get("X-Kalillac-Registration-Nonce")}, {"Cache-Control": "no-store"}
    monkeypatch.setattr(api_class, "handle", handle)
