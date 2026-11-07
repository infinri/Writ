package sjson

import (
	"strings"
	"testing"
)

func TestParseErrors(t *testing.T) {
	cases := map[string]string{
		`{"a":1,"a":1}`:        ErrDuplicateKey,
		`{"x":{"b":1,"b":2}}`:  ErrDuplicateKey,
		`{"a":1,"\u0061":2}`:   ErrDuplicateKey,
		`1.0`:                  ErrNotInteger,
		`1e3`:                  ErrNotInteger,
		`[-0.5]`:               ErrNotInteger,
		`-0`:                   ErrNegativeZero,
		`9007199254740992`:     ErrOutOfRange,
		`-9007199254740992`:    ErrOutOfRange,
		`18446744073709551616`: ErrOutOfRange,
		`01`:                   ErrInvalidJSON,
		`+1`:                   ErrInvalidJSON,
		`1.`:                   ErrInvalidJSON,
		`NaN`:                  ErrInvalidJSON,
		`Infinity`:             ErrInvalidJSON,
		`[-Infinity]`:          ErrInvalidJSON,
		`[1,]`:                 ErrInvalidJSON,
		`{} {}`:                ErrInvalidJSON,
		`"a` + "\x01" + `"`:    ErrInvalidJSON,
		`"\x"`:                 ErrInvalidJSON,
		`"\ud800"`:             ErrLoneSurrogate,
		`"\udc00"`:             ErrLoneSurrogate,
		`"\ud800\u0041"`:       ErrLoneSurrogate,
		"\"\xff\"":             ErrInvalidJSON,
		strings.Repeat("[", 65) + "0" + strings.Repeat("]", 65):       ErrDepthExceeded,
		strings.Repeat("[", 66) + strings.Repeat("]", 66):             ErrDepthExceeded,
		strings.Repeat("[", 64) + `{"k":0}` + strings.Repeat("]", 64): ErrDepthExceeded,
	}
	for in, want := range cases {
		_, err := Parse(in)
		if err == nil || err.Code != want {
			t.Errorf("Parse(%.40q) = %v, want %s", in, err, want)
		}
	}
}

func TestCanonical(t *testing.T) {
	cases := map[string]string{
		`{"b": [1, true, null], "a": "é\n", "c": {"z": 0, "y": "x"}}`:  `{"a":"é\n","b":[1,true,null],"c":{"y":"x","z":0}}`,
		`{"b":"\u00e9","a":"\/","\u00e9":0}`:                           `{"a":"/","b":"é","é":0}`,
		`"\u0001\u001f\t\"\\"`:                                         `"\u0001\u001f\t\"\\"`,
		`"\ud83d\ude00"`:                                               `"😀"`,
		`9007199254740991`:                                             `9007199254740991`,
		`-9007199254740991`:                                            `-9007199254740991`,
		strings.Repeat("[", 65) + strings.Repeat("]", 65):              strings.Repeat("[", 65) + strings.Repeat("]", 65),
		strings.Repeat("[", 63) + `{"k":[]}` + strings.Repeat("]", 63): strings.Repeat("[", 63) + `{"k":[]}` + strings.Repeat("]", 63),
	}
	for in, want := range cases {
		v, err := Parse(in)
		if err != nil {
			t.Fatalf("Parse(%.40q): %v", in, err)
		}
		got, cerr := Canonical(v)
		if cerr != nil || string(got) != want {
			t.Errorf("Canonical(%.40q) = %q, %v; want %q", in, got, cerr, want)
		}
	}
}

func TestKnownVectorDigest(t *testing.T) {
	v, err := Parse(`{"b": [1, true, null], "a": "é\n", "c": {"z": 0, "y": "x"}}`)
	if err != nil {
		t.Fatal(err)
	}
	const want = "sha256:c25cf6d8af15edaf7f9ef3f86172767d36bfdfb090c8eebac9abecca9737980b"
	if got := MustDigest(v); got != want {
		t.Errorf("digest %s, want %s", got, want)
	}
}

func TestCheckDepthMatchesParser(t *testing.T) {
	for _, in := range []string{
		strings.Repeat("[", 64) + "0" + strings.Repeat("]", 64),
		strings.Repeat("[", 65) + "0" + strings.Repeat("]", 65),
		strings.Repeat("[", 63) + `{"k":0}` + strings.Repeat("]", 63),
		strings.Repeat("[", 64) + `{"k":0}` + strings.Repeat("]", 64),
	} {
		_, strict := Parse(in)
		v, err := ParseUnlimited(in)
		if err != nil {
			t.Fatal(err)
		}
		if (strict == nil) != (CheckDepth(v) == nil) {
			t.Errorf("depth disagreement for %.70q", in)
		}
	}
}
