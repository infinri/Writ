"""Output compression (roadmap 18a): drop the redundant bulk, keep the shape.

Measured over 2,367 PostToolUse envelopes: Edit and Write are 95% of all
tool-response bytes, and within an Edit response `originalFile` is 96% of it (the
whole pre-edit file, echoed back) while `structuredPatch` carries the change in
348 chars. Bash has a 742-char median and is not a target.

The load-bearing test is the preserved-key contract. The failure that breaks a
user's edit is a MISSING KEY, not a poor ratio: the hook doc warns the replacement
must match the tool's expected response shape (TEST-ASSERT-001).
"""

from __future__ import annotations

from writ.session.output_compression import MIN_COMPRESSIBLE, compress_tool_response


def _edit_response(original: str = "x" * 30000) -> dict:
    return {
        "filePath": "/repo/a.py",
        "oldString": "before",
        "newString": "after",
        "originalFile": original,
        "structuredPatch": [{"lines": ["-before", "+after"]}],
        "userModified": False,
        "replaceAll": False,
    }


class TestPreservedKeyContract:
    def test_every_original_key_survives(self):
        src = _edit_response()
        out = compress_tool_response("Edit", src)
        assert out is not None
        assert set(out) == set(src), "a missing key breaks the tool's expected shape"

    def test_the_change_itself_is_never_altered(self):
        src = _edit_response()
        out = compress_tool_response("Edit", src)
        for k in ("structuredPatch", "oldString", "newString", "filePath"):
            assert out[k] == src[k], f"{k} must pass through untouched"

    def test_booleans_pass_through(self):
        src = _edit_response()
        out = compress_tool_response("Edit", src)
        assert out["userModified"] is False and out["replaceAll"] is False

    def test_the_input_dict_is_not_mutated(self):
        src = _edit_response()
        compress_tool_response("Edit", src)
        assert len(src["originalFile"]) == 30000


class TestTheMarkerNamesWhatWasDropped:
    def test_marker_reports_the_real_size(self):
        out = compress_tool_response("Edit", _edit_response("y" * 41234))
        assert "41234" in out["originalFile"], out["originalFile"]

    def test_marker_size_tracks_the_input_under_mutation(self):
        a = compress_tool_response("Edit", _edit_response("y" * 20000))["originalFile"]
        b = compress_tool_response("Edit", _edit_response("y" * 40000))["originalFile"]
        assert a != b, "a constant marker would not report the real dropped size"

    def test_marker_names_the_field(self):
        out = compress_tool_response("Edit", _edit_response())
        assert "originalFile" in out["originalFile"]

    def test_the_replacement_is_much_smaller(self):
        out = compress_tool_response("Edit", _edit_response())
        assert len(out["originalFile"]) < 200


class TestNoOpCases:
    def test_a_small_response_is_left_alone(self):
        assert compress_tool_response("Edit", _edit_response("x" * 10)) is None

    def test_a_response_without_the_bulk_field_is_left_alone(self):
        src = _edit_response()
        del src["originalFile"]
        assert compress_tool_response("Edit", src) is None

    def test_an_unknown_tool_is_left_alone(self):
        assert compress_tool_response("Bash", _edit_response()) is None

    def test_a_non_dict_response_is_left_alone(self):
        assert compress_tool_response("Edit", "not a dict") is None

    def test_a_non_string_bulk_field_is_left_alone(self):
        src = _edit_response()
        src["originalFile"] = {"unexpected": "shape"}
        assert compress_tool_response("Edit", src) is None

    def test_exactly_at_the_threshold_is_not_compressed(self):
        assert compress_tool_response("Edit", _edit_response("x" * MIN_COMPRESSIBLE)) is None

    def test_one_byte_over_the_threshold_is_compressed(self):
        assert compress_tool_response("Edit", _edit_response("x" * (MIN_COMPRESSIBLE + 1))) is not None


class TestWriteIsCoveredToo:
    def test_write_responses_compress(self):
        out = compress_tool_response("Write", _edit_response())
        assert out is not None
        assert "originalFile" in out["originalFile"]
