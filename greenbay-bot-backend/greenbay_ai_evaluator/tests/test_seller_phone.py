"""
Tests for the seller phone on POST /tradein/evaluate (Sep 2026, Pulse Sentinel
D08 "Phone captured on 27% of evaluations"):

  - the phone is required: missing, null and blank are all refused
  - Kenyan numbers are accepted as 07.., 01.., bare, +254.., 254.., 00254..,
    with spaces, dashes, dots or parentheses, and stored as 2547XXXXXXXX
  - Uganda / Nigeria numbers and other international numbers still work
  - junk is refused with the message the wizard shows inline
  - the wizard's normalisePhone() in frontend/app.js gives the SAME answer as
    the backend for every case (run under node when node is installed)
  - callers the repo ships keep working: the deploy smoke test payload and the
    WhatsApp (flowcart) caller's phone shape

Deterministic: no network, DB, or LLM calls.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from greenbay_ai_evaluator.api.schemas import (
    PHONE_ERROR_MESSAGE,
    EvaluateRequest,
    normalise_phone,
)

# (input, country, expected stored form or None)
PHONE_CASES: list[tuple[str, str, str | None]] = [
    # Kenya, every shape the brief lists
    ("0712345678", "KE", "254712345678"),
    ("0112345678", "KE", "254112345678"),
    ("+254712345678", "KE", "254712345678"),
    ("254712345678", "KE", "254712345678"),
    ("+254112345678", "KE", "254112345678"),
    ("0712 345 678", "KE", "254712345678"),
    ("0712-345-678", "KE", "254712345678"),
    ("+254 712 345 678", "KE", "254712345678"),
    ("+254-712-345-678", "KE", "254712345678"),
    ("(0712) 345.678", "KE", "254712345678"),
    ("  0712345678  ", "KE", "254712345678"),
    ("712345678", "KE", "254712345678"),
    ("00254712345678", "KE", "254712345678"),
    ("+254 0712 345 678", "KE", "254712345678"),   # common: code AND leading 0
    ("2540712345678", "KE", "254712345678"),
    ("<b>0712345678</b>", "KE", "254712345678"),
    # A Kenyan number is Kenyan whatever country the browser guessed
    ("+254712345678", "UG", "254712345678"),
    ("254712345678", "NG", "254712345678"),
    # Kenya, refused
    ("", "KE", None),
    ("   ", "KE", None),
    ("07123456", "KE", None),            # too short
    ("07123456789", "KE", None),         # too long
    ("0212345678", "KE", None),          # not 07 / 01
    ("0812345678", "KE", None),
    ("+25471234567", "KE", None),        # 254 but one digit short
    ("+2547123456789", "KE", None),      # 254 but one digit long
    ("+254812345678", "KE", None),       # 254 but not 7 / 1
    ("0712345abc", "KE", None),
    ("phone", "KE", None),
    ("+", "KE", None),
    ("++254712345678", "KE", None),
    ("0712345678;DROP", "KE", None),
    ("٠٧١٢٣٤٥٦٧٨", "KE", None),          # non-ASCII digits
    # Uganda
    ("0772123456", "UG", "256772123456"),
    ("+256772123456", "KE", "256772123456"),
    ("256772123456", "KE", "256772123456"),
    ("+256 0772 123456", "UG", "256772123456"),
    ("+25677212345", "UG", None),
    # Nigeria
    ("08031234567", "NG", "2348031234567"),
    ("+2348031234567", "KE", "2348031234567"),
    ("2348031234567", "KE", "2348031234567"),
    ("+2346031234567", "NG", None),
    # Elsewhere: only with an explicit international prefix
    ("+447911123456", "KE", "447911123456"),
    ("00447911123456", "KE", "447911123456"),
    ("447911123456", "KE", "447911123456"),   # WhatsApp sender id: no "+"
    ("4479111234", "KE", None),               # 10 digits, no prefix: not readable
    ("1234567890123456", "KE", None),         # over 15 digits
    ("+1234567", "KE", None),            # under 8 digits
    ("+1234567890123456", "KE", None),   # over 15 digits
    ("+0712345678", "KE", None),
    # As typed: pasted invisibles, odd spaces, and the 30-character limit
    ("\ufeff0712345678", "KE", "254712345678"),
    ("0712\u00a0345\u2009678", "KE", "254712345678"),
    ("0-7-1-2-3-4-5-6-7-8----------", "KE", "254712345678"),            # 29 typed
    ("0-7-1-2-3-4-5-6-7-8-----------------", "KE", None),             # 36 typed
    ("  " + "0712345678" + " " * 40, "KE", "254712345678"),            # outer spaces do not count
    ("０７１２３４５６７８", "KE", None),                                    # fullwidth digits
    # Unknown country code falls back to the Kenyan local rules
    ("0712345678", "TZ", "254712345678"),
    ("0712345678", "", "254712345678"),
]


def _body(**extra) -> dict:
    body = {
        "category": "tv_monitor",
        "brand": "Samsung",
        "condition_grade": "B",
        "condition_score": 75,
        "retail_price": 45000,
        "seller_phone": "0712345678",
    }
    body.update(extra)
    return body


class TestNormalisePhone:
    @pytest.mark.parametrize("raw,country,expected", PHONE_CASES)
    def test_cases(self, raw, country, expected):
        assert normalise_phone(raw, country) == expected

    def test_none(self):
        assert normalise_phone(None) is None

    def test_default_country_is_kenya(self):
        assert normalise_phone("0712345678") == "254712345678"

    def test_non_string_input(self):
        assert normalise_phone(254712345678) == "254712345678"
        assert normalise_phone(712345678) == "254712345678"

    def test_normalising_twice_is_stable(self):
        for raw, country, expected in PHONE_CASES:
            if expected:
                assert normalise_phone(expected, country) == expected

    def test_stored_form_fits_the_column(self):
        # valuation_sessions.seller_phone is String(20)
        for _raw, _country, expected in PHONE_CASES:
            if expected:
                assert len(expected) <= 15 and expected.isdigit()


class TestEvaluateRequestPhone:
    def test_phone_is_stored_normalised(self):
        assert EvaluateRequest(**_body(seller_phone="0712 345-678")).seller_phone == "254712345678"

    def test_three_spellings_one_number(self):
        spellings = ["0712345678", "+254 712 345 678", "254712345678"]
        assert {EvaluateRequest(**_body(seller_phone=p)).seller_phone for p in spellings} == {"254712345678"}

    def test_local_number_is_read_with_the_request_country(self):
        assert EvaluateRequest(**_body(seller_phone="0772123456", country="UG")).seller_phone == "256772123456"

    def test_missing_phone_is_refused(self):
        body = _body()
        del body["seller_phone"]
        with pytest.raises(ValidationError) as exc:
            EvaluateRequest(**body)
        assert "seller_phone" in str(exc.value)

    @pytest.mark.parametrize("bad", [None, "", "   ", "12345", "not a phone", "0212345678"])
    def test_bad_phone_is_refused_with_the_inline_message(self, bad):
        with pytest.raises(ValidationError) as exc:
            EvaluateRequest(**_body(seller_phone=bad))
        text = str(exc.value)
        assert "seller_phone" in text
        assert PHONE_ERROR_MESSAGE in text

    def test_error_is_a_422_that_names_the_phone(self):
        # The wizard routes a 422 whose detail mentions "phone" back to the
        # contact step instead of showing a demo offer.
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()

        @app.post("/evaluate")
        def evaluate(req: EvaluateRequest):
            return {"seller_phone": req.seller_phone}

        client = TestClient(app)
        ok = client.post("/evaluate", json=_body(seller_phone="+254 712 345 678"))
        assert ok.status_code == 200
        assert ok.json() == {"seller_phone": "254712345678"}
        for bad in (_body(seller_phone="12345"), _body(seller_phone=None),
                    {k: v for k, v in _body().items() if k != "seller_phone"}):
            r = client.post("/evaluate", json=bad)
            assert r.status_code == 422
            # Exactly what the wizard checks: loc or msg, never the echoed input.
            assert any(
                "seller_phone" in e.get("loc", []) or "seller_phone" in e.get("msg", "")
                for e in r.json()["detail"]
            )

    def test_over_long_phone_is_a_phone_error_too(self):
        with pytest.raises(ValidationError) as exc:
            EvaluateRequest(**_body(seller_phone="0-7-1-2-3-4-5-6-7-8-----------------"))
        assert "seller_phone" in str(exc.value)

    def test_wizard_input_cannot_exceed_the_api_limit(self):
        from greenbay_ai_evaluator.api.schemas import PHONE_MAX_INPUT_LEN
        html = (_APP_JS.parent / "index.html").read_text(encoding="utf-8")
        tag = re.search(r'<input[^>]*id="sellerPhoneInput"[^>]*>', html, re.DOTALL).group(0)
        assert f'maxlength="{PHONE_MAX_INPUT_LEN}"' in tag
        assert EvaluateRequest.model_fields["seller_phone"].metadata  # max_length is declared

    def test_deploy_smoke_test_payload_still_validates(self):
        # .github/workflows/deploy.yml posts this body after every deploy.
        req = EvaluateRequest(**{
            "category": "tv_monitor", "brand": "Samsung", "model": "ZZSMOKE-CI",
            "age_years": 2, "condition_grade": "B", "condition_score": 70,
            "retail_price": 50000, "retail_price_source": "category_default",
            "seller_name": "ZZSMOKE CI", "seller_phone": "+254700000000", "country": "KE",
        })
        assert req.seller_phone == "254700000000"

    @pytest.mark.parametrize("wa_from,country,expected", [
        ("254712345678", "KE", "254712345678"),
        ("+254712345678", "KE", "254712345678"),
        ("256772123456", "UG", "256772123456"),
        ("2348031234567", "NG", "2348031234567"),
        ("447911123456", "KE", "447911123456"),   # diaspora customer on WhatsApp
    ])
    def test_whatsapp_caller_phone_shapes(self, wa_from, country, expected):
        # app/webhooks/flowcart.py sends the WhatsApp sender id as seller_phone.
        assert EvaluateRequest(**_body(seller_phone=wa_from, country=country)).seller_phone == expected


# ---------------------------------------------------------------------------
# Frontend parity: frontend/app.js normalisePhone() must agree with the backend
# ---------------------------------------------------------------------------
_APP_JS = Path(__file__).resolve().parents[2] / "frontend" / "app.js"


def _frontend_phone_source() -> str:
    src = _APP_JS.read_text(encoding="utf-8")
    start = src.index("const PHONE_RULES = {")
    end = src.index("function isValidPhone(")
    return src[start:end]


class TestFrontendParity:
    def test_frontend_declares_the_same_country_rules(self):
        from greenbay_ai_evaluator.api.schemas import _PHONE_RULES

        src = _frontend_phone_source()
        for country, (code, national) in _PHONE_RULES.items():
            js_pattern = national.replace("\\", "\\\\")
            assert f"{country}: ['{code}', '{js_pattern}']" in src

    def test_wizard_shows_the_backend_message(self):
        html = (_APP_JS.parent / "index.html").read_text(encoding="utf-8")
        assert " ".join(PHONE_ERROR_MESSAGE.split()) in " ".join(html.split())

    @pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
    def test_frontend_and_backend_agree_on_every_case(self):
        script = (
            _frontend_phone_source()
            + "\nconst cases = JSON.parse(require('fs').readFileSync(0, 'utf8'));\n"
            + "console.log(JSON.stringify(cases.map(c => normalisePhone(c[0], c[1]))));\n"
        )
        cases = [[raw, country] for raw, country, _ in PHONE_CASES]
        out = subprocess.run(
            ["node", "-e", script], input=json.dumps(cases),
            capture_output=True, text=True, timeout=30, check=True,
        )
        got = json.loads(out.stdout)
        expected = [e for _, _, e in PHONE_CASES]
        mismatches = [
            (c, g, e) for c, g, e in zip(cases, got, expected) if g != e
        ]
        assert mismatches == []
