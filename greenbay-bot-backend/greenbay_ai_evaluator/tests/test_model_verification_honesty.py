"""`model_verified` must mean a photo confirmed it — never that the user typed it.

The frontend renders "Model X verified!" straight off this flag, so echoing the
typed value back as verified tells the customer their label was checked when no
vision call happened. Same class of defect as the deleted `_stub_analysis` and
the deleted `generateDemoResults`: a confident claim manufactured from a failure.
"""

from __future__ import annotations

import greenbay_ai_evaluator.services.vision_service as vs


class TestUnconfiguredNeverReportsVerified:
    def test_no_api_key_is_not_verified(self):
        out = vs.analyze_model_label("data:image/jpeg;base64,AAAA", typed_model="RT34K5532S8", api_key="")
        assert out["model_verified"] is False

    def test_typed_model_is_echoed_but_not_blessed(self):
        out = vs.analyze_model_label("data:image/jpeg;base64,AAAA", typed_model="RT34K5532S8", api_key="")
        assert out["model_number"] == "RT34K5532S8"
        assert out["model_verified"] is False

    def test_no_specs_summary_is_invented(self):
        out = vs.analyze_model_label("data:image/jpeg;base64,AAAA", typed_model="RT34K5532S8", api_key="")
        assert out["specs_summary"] is None

    def test_empty_typed_model_is_not_verified(self):
        out = vs.analyze_model_label("data:image/jpeg;base64,AAAA", typed_model="", api_key="")
        assert out["model_verified"] is False


class TestApiFailureNeverReportsVerified:
    def test_exception_path_is_not_verified(self, monkeypatch):
        class _Boom:
            def __init__(self, *a, **k):
                raise RuntimeError("billing exhausted")

        monkeypatch.setattr(vs, "HAS_ANTHROPIC", True, raising=False)
        monkeypatch.setattr(vs, "anthropic", type("M", (), {"Anthropic": _Boom}), raising=False)
        out = vs.analyze_model_label(
            "data:image/jpeg;base64,AAAA", typed_model="RT34K5532S8", api_key="sk-ant-test"
        )
        assert out["model_verified"] is False
        assert out["specs_summary"] is None


class TestSourceHasNoResurrectedStub:
    """Guard the literal pattern, so a future edit cannot quietly restore it."""

    def test_verified_is_never_derived_from_typed_model(self):
        import inspect
        src = inspect.getsource(vs.analyze_model_label)
        assert '"model_verified": bool(typed_model)' not in src
        assert "'model_verified': bool(typed_model)" not in src
