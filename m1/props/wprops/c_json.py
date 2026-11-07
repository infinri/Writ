"""Campaign `json`: strict parsing and D-006 canonicalization (command `canonical`),
plus cross-command consistency of strict-parse failures.

Oracles (spec: acceptance/SEMANTICS.md "Strict JSON", D-006, D-017, D-018 A1):
- generated valid values: canonical bytes equal the D-006 form of the generated
  value and digest is sha256 of those bytes, whatever key order, whitespace or
  escape spelling was used;
- canonical output fed back is a fixed point;
- exactly one injected fault per case gives the error code the spec assigns;
  where the spec does not pin the code (for example `-0.0`) only rejection is
  required and the allowed set is recorded;
- the same malformed text gives the same strict-parse code in `canonical`,
  `check-acceptance` and `transition-scenario`, and REQUEST_JSON_INVALID in `decide`.
"""
from __future__ import annotations

import unicodedata

import json

from .common import MAX_SAFE, Results, canonical_text, differential, dump_string, max_depth, run_both, text_digest

NAME = "json"


class Raw:
    def __init__(self, text):
        self.text = text


class Pairs:
    """Object with explicit (raw key text, value) members, allowing duplicates."""

    def __init__(self, pairs):
        self.pairs = pairs


WS = [" ", "\t", "\n", "\r"]


def ser(v, rng, p_ws=0.25, p_esc=0.1):
    w = (lambda: rng.choice(WS) if rng.random() < p_ws else "")
    if isinstance(v, Raw):
        return v.text
    if isinstance(v, Pairs):
        return "{" + ",".join(w() + k + w() + ":" + w() + ser(x, rng, p_ws, p_esc) + w() for k, x in v.pairs) + "}"
    if isinstance(v, bool) or v is None:
        return {True: "true", False: "false", None: "null"}[v]
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return dump_string(v, rng, p_esc)
    if isinstance(v, list):
        return "[" + w() + ",".join(w() + ser(x, rng, p_ws, p_esc) + w() for x in v) + w() + "]"
    if isinstance(v, dict):
        keys = list(v)
        rng.shuffle(keys)
        return "{" + w() + ",".join(w() + dump_string(k, rng, p_esc) + w() + ":" + w() + ser(v[k], rng, p_ws, p_esc) + w()
                                   for k in keys) + w() + "}"
    raise TypeError(type(v))


CHARS = ["a", "b", "k", "Z", "0", "_", "-", " ", "é", "é", "ß", "ÿ", "Ā", " ", "﻿",
         "编", "\U0001F600", "\U00010000", "\U0010FFFF", "\"", "\\", "/", "\n", "\t", "\x00", "\x1f", "\x7f",
         "퟿", "", "￿"]


def rand_str(rng, maxlen=8):
    return "".join(rng.choice(CHARS) for _ in range(rng.randint(0, maxlen)))


def rand_int(rng):
    r = rng.random()
    if r < 0.15:
        return rng.choice([0, 1, -1, MAX_SAFE, -MAX_SAFE, MAX_SAFE - 1, -(MAX_SAFE - 1), 2**31, -2**31, 2**32, 2**52])
    if r < 0.5:
        return rng.randint(-1000, 1000)
    return rng.randint(-MAX_SAFE, MAX_SAFE)


def rand_value(rng, depth=0, maxd=5):
    r = rng.random()
    if depth >= maxd or r < 0.45:
        return rng.choice([lambda: rand_int(rng), lambda: rand_str(rng), lambda: True, lambda: False,
                           lambda: None])()
    if r < 0.72:
        return [rand_value(rng, depth + 1, maxd) for _ in range(rng.randint(0, 4))]
    d = {}
    for _ in range(rng.randint(0, 5)):
        d[rand_str(rng, 5)] = rand_value(rng, depth + 1, maxd)
    return d


def containers(v, path=()):
    """Yield (path, container) for every list/dict in v."""
    if isinstance(v, (list, dict)):
        yield path, v
        items = enumerate(v) if isinstance(v, list) else v.items()
        for k, x in items:
            yield from containers(x, path + (k,))


def replace_at(root, path, new):
    if not path:
        return new
    cur = root
    for k in path[:-1]:
        cur = cur[k]
    cur[path[-1]] = new
    return root


def value_paths(v, path=()):
    yield path
    if isinstance(v, list):
        for i, x in enumerate(v):
            yield from value_paths(x, path + (i,))
    elif isinstance(v, dict):
        for k, x in v.items():
            yield from value_paths(x, path + (k,))


def escape_spelling(key: str, rng) -> str:
    """A different JSON spelling of the same key (at least one char escaped)."""
    if not key:
        return None
    i = rng.randrange(len(key))
    out = []
    for j, ch in enumerate(key):
        if j == i:
            cp = ord(ch)
            if cp > 0xFFFF:
                v = cp - 0x10000
                out.append("\\u%04x\\u%04x" % (0xD800 + (v >> 10), 0xDC00 + (v & 0x3FF)))
            else:
                out.append("\\u%04X" % cp if rng.random() < 0.5 else "\\u%04x" % cp)
        else:
            out.append(dump_string(ch, rng, 0.0)[1:-1])
    return '"' + "".join(out) + '"'


# number tokens: (token, allowed error codes or None when valid)
NUM_FAULTS = [
    ("1.0", {"NUMBER_NOT_INTEGER"}), ("0.5", {"NUMBER_NOT_INTEGER"}), ("-1.5", {"NUMBER_NOT_INTEGER"}),
    ("1e0", {"NUMBER_NOT_INTEGER"}), ("1E5", {"NUMBER_NOT_INTEGER"}), ("1e-3", {"NUMBER_NOT_INTEGER"}),
    ("10E+2", {"NUMBER_NOT_INTEGER"}), ("0.0", {"NUMBER_NOT_INTEGER"}), ("7.000", {"NUMBER_NOT_INTEGER"}),
    ("-0", {"NUMBER_NEGATIVE_ZERO"}),
    (str(2**53), {"NUMBER_OUT_OF_RANGE"}), (str(-(2**53)), {"NUMBER_OUT_OF_RANGE"}),
    (str(2**64), {"NUMBER_OUT_OF_RANGE"}), (str(2**64 + 1), {"NUMBER_OUT_OF_RANGE"}),
    (str(2**63), {"NUMBER_OUT_OF_RANGE"}), (str(-(2**63) - 1), {"NUMBER_OUT_OF_RANGE"}),
    (str(10**30), {"NUMBER_OUT_OF_RANGE"}), (str(2**53 + 2), {"NUMBER_OUT_OF_RANGE"}),
    ("01", {"INVALID_JSON"}), ("-01", {"INVALID_JSON"}), ("00", {"INVALID_JSON"}), ("+1", {"INVALID_JSON"}),
    (".5", {"INVALID_JSON"}), ("1.", {"INVALID_JSON"}), ("-", {"INVALID_JSON"}), ("1e", {"INVALID_JSON"}),
    ("0x10", {"INVALID_JSON"}), ("1_000", {"INVALID_JSON"}), ("--1", {"INVALID_JSON"}), ("-00", {"INVALID_JSON"}),
]
# the spec rejects these but does not say which code wins
NUM_AMBIGUOUS = [
    ("-0.0", {"NUMBER_NOT_INTEGER", "NUMBER_NEGATIVE_ZERO"}), ("-0e0", {"NUMBER_NOT_INTEGER", "NUMBER_NEGATIVE_ZERO"}),
    ("9007199254740992.0", {"NUMBER_NOT_INTEGER", "NUMBER_OUT_OF_RANGE"}),
    ("1e400", {"NUMBER_NOT_INTEGER", "NUMBER_OUT_OF_RANGE"}), ("1e30", {"NUMBER_NOT_INTEGER", "NUMBER_OUT_OF_RANGE"}),
    ("-0.5e999999999999999999", {"NUMBER_NOT_INTEGER", "NUMBER_OUT_OF_RANGE"}),
]
LITERALS = ["NaN", "Infinity", "-Infinity", "nan", "inf", "-inf", "undefined", "True", "None", "nul", "truee"]
SURR_BAD = ["\\ud800", "\\udbff", "\\udc00", "\\udfff", "\\ude00\\ud83d", "\\ud800\\u0041", "\\ud800x",
            "\\uD83D", "\\ud83d\\ud83d", "a\\udc00b"]
SURR_OK = [("\\ud83d\\ude00", "\U0001F600"), ("\\uD83D\\uDE00", "\U0001F600"), ("\\ud800\\udc00", "\U00010000"),
           ("\\udbff\\udfff", "\U0010FFFF")]
SYNTAX = [
    ("trailing_comma_array", "[1,2,]"), ("trailing_comma_object", '{"a":1,}'), ("missing_colon", '{"a" 1}'),
    ("unquoted_key", "{a:1}"), ("single_quotes", "{'a':1}"), ("trailing_garbage", "{} x"),
    ("two_values", "1 2"), ("empty", ""), ("only_ws", " \n\t"), ("comment", '{"a":1 /* c */}'),
    ("line_comment", '[1] // c'), ("unterminated_string", '"abc'), ("unterminated_array", "[1,2"),
    ("unterminated_object", '{"a":1'), ("bad_escape", '"\\x41"'), ("short_unicode_escape", '"\\u12"'),
    ("nbsp_ws", "[1, 2]"), ("formfeed_ws", "[1,\f2]"), ("vtab_ws", "[1,\v2]"), ("raw_tab_in_string", '"a\tb"'),
    ("raw_newline_in_string", '"a\nb"'), ("raw_nul_in_string", '"a\x00b"'), ("leading_comma", "[,1]"),
    ("double_comma", "[1,,2]"), ("colon_in_array", "[1:2]"), ("key_not_string", "{1:2}"),
    ("uppercase_literal", "TRUE"), ("unicode_escape_upper_U", '"\\U0041"'),
]
AMBIG_SYNTAX = [("bom_prefix", "﻿{}")]  # RFC 8259 lets parsers ignore a BOM; the M1 spec is silent


def gen_cases(rng, n):
    cases, k = [], 0

    def add(kind, text, expect, **meta):
        nonlocal k
        k += 1
        c = {"id": f"json-{k:06d}-{kind}", "command": "canonical", "input_text": text,
             "meta": {"kind": kind, "expect": expect, **meta}}
        cases.append(c)
        return c

    kinds = ["valid", "valid", "dup", "dup_escaped", "near_dup", "num_fault", "num_ambiguous", "num_valid",
             "literal", "surrogate_bad", "surrogate_ok", "depth", "syntax", "large", "ambiguous_syntax"]
    weights = [16, 4, 8, 8, 4, 12, 3, 4, 4, 6, 4, 14, 6, 1, 1]
    while len(cases) < n:
        kind = rng.choices(kinds, weights)[0]
        if kind == "valid":
            v = rand_value(rng)
            group = f"g{k}"
            for _ in range(rng.randint(1, 3)):
                add("valid", ser(v, rng, p_ws=rng.random() * 0.6, p_esc=rng.random() * 0.5),
                    {"canonical": canonical_text(v)}, group=group)
            add("fixed_point", canonical_text(v), {"canonical": canonical_text(v)}, group=group)
        elif kind in ("dup", "dup_escaped"):
            v = {rand_str(rng, 4) or "k": rand_value(rng, 1, 3)} if rng.random() < 0.3 else rand_value(rng, 0, 4)
            objs = [(p, c) for p, c in containers(v) if isinstance(c, dict) and c]
            if not objs:
                v = {"wrap": v, rand_str(rng, 3) + "x": 1}
                objs = [((), v)]
            path, obj = rng.choice(objs)
            key = rng.choice(list(obj))
            if kind == "dup_escaped":
                spelled = escape_spelling(key, rng)
                if spelled is None:
                    continue
            else:
                spelled = dump_string(key, rng, 0.0)
            pairs = [(dump_string(kk, rng, 0.2), vv) for kk, vv in obj.items()]
            dupval = obj[key] if rng.random() < 0.4 else rand_value(rng, 2, 3)
            pairs.insert(rng.randint(0, len(pairs)), (spelled, dupval))
            tree = replace_at(v, path, Pairs(pairs)) if path else Pairs(pairs)
            add(kind, ser(tree, rng), {"error": {"DUPLICATE_KEY"}}, depth_of_dup=len(path))
        elif kind == "near_dup":
            base = rng.choice(["é", "a", "Key", "x "])
            other = {"é": "é", "a": "A", "Key": "key", "x ": "x"}[base]
            v = {base: rand_value(rng, 1, 2), other: rand_value(rng, 1, 2)}
            if rng.random() < 0.5:
                v = {"outer": v}
            add("near_dup", ser(v, rng), {"canonical": canonical_text(v)})
        elif kind in ("num_fault", "num_ambiguous", "num_valid", "literal"):
            v = rand_value(rng, 0, 3)
            if kind == "num_valid":
                tok_v = rng.choice([MAX_SAFE, -MAX_SAFE, 0, rng.randint(-MAX_SAFE, MAX_SAFE)])
                tok, exp = str(tok_v), None
            elif kind == "literal":
                tok, exp = rng.choice(LITERALS), {"INVALID_JSON"}
            else:
                tok, exp = rng.choice(NUM_FAULTS if kind == "num_fault" else NUM_AMBIGUOUS)
            path = rng.choice(list(value_paths(v)))
            if exp is None:
                v2 = replace_at(v, path, tok_v)
                add(kind, ser(v2, rng), {"canonical": canonical_text(v2)})
            else:
                tree = replace_at(v, path, Raw(tok))
                add(kind, ser(tree, rng), {"error": exp}, token=tok)
        elif kind in ("surrogate_bad", "surrogate_ok"):
            v = rand_value(rng, 0, 3)
            in_key = rng.random() < 0.3
            if kind == "surrogate_bad":
                esc, exp = rng.choice(SURR_BAD), {"error": {"LONE_SURROGATE"}}
            else:
                esc, ch = rng.choice(SURR_OK)
            prefix, suffix = rand_str(rng, 2).replace("\\", "").replace('"', ""), ""
            raw = '"' + dump_string(prefix, rng, 0.0)[1:-1] + esc + '"'
            if in_key:
                v2 = Pairs([(raw, rand_value(rng, 1, 2))])
                tree = v2 if rng.random() < 0.5 else [v2]
                if kind == "surrogate_ok":
                    plain = {prefix + ch: None}
                    tree_plain = plain if isinstance(tree, Pairs) else [plain]
                    # rebuild with the same value so expected output is exact
                    val = v2.pairs[0][1]
                    plain[prefix + ch] = val
                    add(kind, ser(tree, rng), {"canonical": canonical_text(tree_plain)})
                else:
                    add(kind, ser(tree, rng), exp, escape=esc)
            else:
                path = rng.choice(list(value_paths(v)))
                tree = replace_at(v, path, Raw(raw))
                if kind == "surrogate_ok":
                    expected = replace_at(_plainify(tree), path, prefix + ch) if path else prefix + ch
                    add(kind, ser(tree, rng), {"canonical": canonical_text(expected)})
                else:
                    add(kind, ser(tree, rng), exp, escape=esc)
        elif kind == "depth":
            target = rng.randint(58, 67)
            v = build_chain(rng, target)
            d = max_depth(v)
            exp = {"canonical": canonical_text(v)} if d <= 64 else {"error": {"DEPTH_EXCEEDED"}}
            add("depth", ser(v, rng, p_ws=0.1), exp, max_depth=d)
        elif kind == "syntax":
            name, text = rng.choice(SYNTAX)
            if rng.random() < 0.5 and name not in ("empty", "only_ws", "two_values", "trailing_garbage"):
                text = '{"wrap":[' + text + "]}"
            add("syntax", text, {"error": {"INVALID_JSON"}}, syntax=name)
        elif kind == "ambiguous_syntax":
            name, text = rng.choice(AMBIG_SYNTAX)
            add("ambiguous_syntax", text, {"either": [{"canonical": "{}"}, {"error": {"INVALID_JSON"}}]}, syntax=name)
        elif kind == "large":
            r = rng.random()
            if r < 0.33:
                v = [rand_int(rng) for _ in range(rng.randint(2000, 20000))]
            elif r < 0.66:
                v = "".join(rng.choice(CHARS) for _ in range(rng.randint(20000, 100000)))
            else:
                v = {f"k{i}-{rand_str(rng, 2)}": rand_int(rng) for i in range(rng.randint(500, 3000))}
            add("large", ser(v, rng, p_ws=0.05, p_esc=0.05), {"canonical": canonical_text(v)})
    return cases


def _plainify(tree):
    """Convert a tree that may hold Raw/Pairs into plain JSON values (Raw -> None placeholder)."""
    if isinstance(tree, Raw):
        return None
    if isinstance(tree, Pairs):
        raise ValueError("pairs in plain tree")
    if isinstance(tree, list):
        return [_plainify(x) for x in tree]
    if isinstance(tree, dict):
        return {k: _plainify(x) for k, x in tree.items()}
    return tree


def build_chain(rng, length):
    """`length` nested containers (mixed), with a random kind of innermost leaf."""
    leaf = rng.choice(["scalar", "empty_array", "empty_object", "member", "string", "array_scalar"])
    v = {"scalar": 0, "empty_array": [], "empty_object": {}, "member": {"k": 0}, "string": "s",
         "array_scalar": [0]}[leaf]
    style = rng.choice(["arrays", "objects", "alternate", "random"])
    for i in range(length):
        if style == "arrays":
            kind = "a"
        elif style == "objects":
            kind = "o"
        elif style == "alternate":
            kind = "a" if i % 2 else "o"
        else:
            kind = rng.choice("ao")
        if kind == "a":
            v = [v] if rng.random() < 0.8 else [0, v, "x"]
        else:
            v = {"k": v} if rng.random() < 0.8 else {"a": 1, "k": v}
    return v


def check_one(case, out):
    """Return (invariant, ok, detail) for one canonical output."""
    exp, kind = case["meta"]["expect"], case["meta"]["kind"]
    inv = f"json.{kind}"
    if not isinstance(out, dict):
        return inv, False, "no output"
    if "either" in exp:
        alts = exp["either"]
        ok = any(("canonical" in a and out.get("canonical") == a["canonical"]) or
                 ("error" in a and out.get("error") in a["error"]) for a in alts)
        return inv, ok, out
    if "canonical" in exp:
        ok = (set(out) == {"canonical", "digest"} and out.get("canonical") == exp["canonical"]
              and out.get("digest") == text_digest(exp["canonical"]))
        return inv, ok, {"got": out, "want_canonical": exp["canonical"][:400]}
    ok = set(out) == {"error"} and out.get("error") in exp["error"]
    return inv, ok, {"got": out, "allowed": sorted(exp["error"])}


ENVELOPES = [
    ("not_json", b"this is not json"), ("array", b"[1,2]"), ("missing_id", b'{"input_text":"1"}'),
    ("numeric_id", b'{"id":7,"input_text":"1"}'), ("both_inputs", b'{"id":"e","input":1,"input_text":"1"}'),
    ("neither_input", b'{"id":"e"}'), ("input_text_not_string", b'{"id":"e","input_text":1}'),
    ("duplicate_id_key", b'{"id":"e","id":"f","input_text":"1"}'), ("extra_member", b'{"id":"e","input_text":"1","x":1}'),
    ("nan_in_envelope", b'{"id":"e","input":NaN}'), ("invalid_utf8", b'{"id":"e","input_text":"\xff\xfe"}'),
    ("raw_invalid_utf8", b'{"id":"e","input_text":"\xc3\x28"}'.replace(b"\\xc3\\x28", b"\xc3\x28")),
    ("lone_surrogate_in_envelope", b'{"id":"e","input_text":"\\ud800"}'.replace(b"\\\\", b"\\")),
    ("empty_line", b""), ("whitespace_line", b"   "), ("crlf", b'{"id":"e","input_text":"1"}\r'),
]


# D-021 expected answer per malformed probe, written by hand from the rule: the id is
# preserved only when the line parses strictly as JSON (duplicate keys, NaN, invalid
# UTF-8 and lone surrogates fail) and has exactly one string "id"; otherwise null.
# "ignored": blank lines get no answer. Probes not listed are valid envelopes.
D021_ENVELOPE = {
    "not_json": None, "array": None, "missing_id": None, "numeric_id": None,
    "both_inputs": "e", "neither_input": "e", "input_text_not_string": "e",
    "duplicate_id_key": None, "extra_member": "e", "nan_in_envelope": None,
    "invalid_utf8": None, "raw_invalid_utf8": None, "lone_surrogate_in_envelope": None,
    "empty_line": "ignored", "whitespace_line": "ignored",
}


def run_envelopes(protos, res: Results):
    """Malformed and large envelopes between two well-formed lines (unspecified:
    differential, plus: the well-formed neighbours are still answered correctly)."""
    good = lambda i: json.dumps({"id": f"good-{i}", "input_text": "[1]"}).encode()  # noqa: E731
    probes = list(ENVELOPES) + [("large_5mb", json.dumps({"id": "e", "input_text": "[" + ",".join(["1"] * 2_500_000) + "]"}).encode()),
                                ("deep_envelope", b'{"id":"e","input":' + b"[" * 200 + b"]" * 200 + b"}")]
    obs = {}
    for name, line in probes:
        payload = good(1) + b"\n" + line + b"\n" + good(2) + b"\n"
        per = {}
        for p in protos:
            o, probs = p.run_raw("canonical", payload, ["good-1", "good-2"])
            ok = all(o.get(f"good-{i}") == {"canonical": "[1]", "digest": text_digest("[1]")} for i in (1, 2))
            res.check("json.envelope_neighbours_still_answered", p.name, ok, name, {"outputs": o, "problems": probs[:3]})
            answers = {k: v for k, v in o.items() if not str(k).startswith("good-")}
            per[p.name] = {"answers": answers,
                           "problems": [x for x in probs if "unanswered" not in x and "unknown ids" not in x]}
            if name in D021_ENVELOPE:
                want = D021_ENVELOPE[name]
                expected = {} if want == "ignored" else {want: {"error": "ENVELOPE_INVALID"}}
                exit_ok = not any(x.startswith("exit status") for x in probs)
                res.check("json.d021_envelope_correlation", p.name, answers == expected and exit_ok, name,
                          {"got": answers, "want": expected, "problems": probs[:3]})
        obs[name] = per
        vals = list(per.values())
        if len(vals) == 2:
            same = vals[0]["answers"] == vals[1]["answers"]
            res.check("differential.envelope_answers", "both", same, name)
            if not same:
                res.mismatch({"id": "envelope-" + name, "command": "canonical", "envelope_line": line[:300].decode("utf-8", "replace")},
                             {p.name: per[p.name] for p in protos})
    res.envelopes = obs


def run(protos, rng, n, res: Results, minimizer=None):
    run_envelopes(protos, res)
    cases = gen_cases(rng, n)
    outs = run_both(protos, "canonical", cases, res)
    for p in protos:
        o = outs[p.name]
        groups = {}
        for c in cases:
            out = o.get(c["id"])
            inv, ok, detail = check_one(c, out)
            res.check(inv, p.name, ok, c["id"], detail, case=None if ok else c)
            if isinstance(out, dict) and "canonical" in out:
                res.check("json.digest_is_sha256_of_canonical", p.name,
                          out.get("digest") == text_digest(out["canonical"]), c["id"], out)
            g = c["meta"].get("group")
            if g:
                groups.setdefault(g, []).append(out)
        for g, outs_g in groups.items():
            res.check("json.permutation_whitespace_escape_invariance", p.name,
                      all(x == outs_g[0] for x in outs_g), g, outs_g[:2])
    differential(protos, outs, cases, res)

    # round 2: prototype canonical output re-canonicalized is a fixed point
    for p in protos:
        o = outs[p.name]
        second = [{"id": c["id"] + "-r2", "command": "canonical", "input_text": o[c["id"]]["canonical"]}
                  for c in cases if isinstance(o.get(c["id"]), dict) and "canonical" in o[c["id"]]]
        o2, probs = p.run("canonical", second)
        res.protocol[p.name].extend(probs)
        for c in second:
            first = o[c["id"][:-3]]
            res.check("json.recanonicalization_fixed_point", p.name, o2.get(c["id"]) == first, c["id"],
                      {"first": first, "second": o2.get(c["id"])})

    # cross-command: identical strict-parse codes for the same malformed text
    faults = [c for c in cases if "error" in c["meta"]["expect"]][:max(200, n // 4)]
    for cmd in ("check-acceptance", "transition-scenario", "decide", "broker-scenario"):
        sub = [{"id": c["id"] + "@" + cmd, "command": cmd, "input_text": c["input_text"], "meta": c["meta"]}
               for c in faults]
        o_cmd = run_both(protos, cmd, sub, res)
        for p in protos:
            for c in sub:
                base = outs[p.name].get(c["id"].split("@")[0])
                code = base.get("error") if isinstance(base, dict) else None
                got = o_cmd[p.name].get(c["id"])
                if cmd == "check-acceptance":
                    want = {"refusal": "INPUT_JSON_INVALID", "json_error": code}
                elif cmd == "transition-scenario":
                    want = {"input_error": "INPUT_JSON_INVALID", "json_error": code}
                elif cmd == "decide":
                    want = None
                    ok = isinstance(got, dict) and got.get("status") == "invalid" and \
                        got.get("reason_code") == "REQUEST_JSON_INVALID"
                    res.check("json.cross_command.decide_request_json_invalid", p.name, ok, c["id"], got,
                              case=None if ok else c)
                    continue
                else:
                    continue  # broker-scenario: output shape unspecified; differential only
                ok = got == want and code is not None
                res.check(f"json.cross_command.{cmd}", p.name, ok, c["id"], {"got": got, "want": want},
                          case=None if ok else c)
        differential(protos, o_cmd, sub, res)
    return cases, outs


def extra_report(res):
    return {"envelope_observations": getattr(res, "envelopes", {})}
