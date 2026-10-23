"""A malformed CSV upload is refused with a clear 400, never an internal server error."""
from __future__ import annotations

import pytest

BOOTSTRAP_EMAIL = "admin@test.local"
BOOTSTRAP_PASSWORD = "Admin1234"

PAYLOADS = {
    "stray-cr": b"title\r\x00",
    "oversized-field": b"title\n" + b"x" * 200_000,
    "open-quote": b'a,"b\r\x00',
}


@pytest.mark.parametrize("name", list(PAYLOADS))
def test_malformed_csv_is_a_400_not_a_500(client, name):
    assert client.post("/api/auth/login", json={"email": BOOTSTRAP_EMAIL, "password": BOOTSTRAP_PASSWORD}).status_code == 200
    res = client.post("/api/bugs/import", files={"file": ("bad.csv", PAYLOADS[name], "text/csv")})
    assert res.status_code == 400, res.text
    assert res.json()["detail"]
