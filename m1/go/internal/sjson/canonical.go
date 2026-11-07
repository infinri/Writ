package sjson

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"sort"
	"strconv"
)

// DigestPrefix is the D-006 digest string prefix.
const DigestPrefix = "sha256:"

// Canonical returns the D-006 canonical bytes: keys sorted by code point
// (UTF-8 byte order is code point order), no insignificant whitespace, UTF-8,
// minimal escaping (quote, backslash and control characters only). The value
// domain is the one the strict parser produces; anything else is an error.
func Canonical(v any) ([]byte, error) {
	if err := CheckDepth(v); err != nil {
		return nil, err
	}
	var b []byte
	return appendValue(b, v)
}

// MustCanonical is Canonical for values built by this program (always in domain).
func MustCanonical(v any) []byte {
	b, err := Canonical(v)
	if err != nil {
		panic(fmt.Sprintf("canonical: %v", err))
	}
	return b
}

// Digest returns "sha256:<hex>" of the canonical bytes.
func Digest(v any) (string, error) {
	b, err := Canonical(v)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(b)
	return DigestPrefix + hex.EncodeToString(sum[:]), nil
}

// MustDigest is Digest for values known to be in the canonical domain.
func MustDigest(v any) string {
	d, err := Digest(v)
	if err != nil {
		panic(fmt.Sprintf("digest: %v", err))
	}
	return d
}

func appendValue(b []byte, v any) ([]byte, error) {
	switch t := v.(type) {
	case nil:
		return append(b, "null"...), nil
	case bool:
		if t {
			return append(b, "true"...), nil
		}
		return append(b, "false"...), nil
	case int64:
		if t > MaxSafeInteger || t < -MaxSafeInteger {
			return nil, &Error{ErrOutOfRange}
		}
		return strconv.AppendInt(b, t, 10), nil
	case int:
		return appendValue(b, int64(t))
	case string:
		return appendString(b, t), nil
	case []any:
		b = append(b, '[')
		for i, e := range t {
			if i > 0 {
				b = append(b, ',')
			}
			var err error
			if b, err = appendValue(b, e); err != nil {
				return nil, err
			}
		}
		return append(b, ']'), nil
	case []string:
		b = append(b, '[')
		for i, e := range t {
			if i > 0 {
				b = append(b, ',')
			}
			b = appendString(b, e)
		}
		return append(b, ']'), nil
	case map[string]any:
		keys := make([]string, 0, len(t))
		for k := range t {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		b = append(b, '{')
		for i, k := range keys {
			if i > 0 {
				b = append(b, ',')
			}
			b = appendString(b, k)
			b = append(b, ':')
			var err error
			if b, err = appendValue(b, t[k]); err != nil {
				return nil, err
			}
		}
		return append(b, '}'), nil
	}
	return nil, fmt.Errorf("unsupported type %T", v)
}

const hexDigits = "0123456789abcdef"

func appendString(b []byte, s string) []byte {
	b = append(b, '"')
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch c {
		case '"':
			b = append(b, '\\', '"')
		case '\\':
			b = append(b, '\\', '\\')
		case '\n':
			b = append(b, '\\', 'n')
		case '\r':
			b = append(b, '\\', 'r')
		case '\t':
			b = append(b, '\\', 't')
		case '\b':
			b = append(b, '\\', 'b')
		case '\f':
			b = append(b, '\\', 'f')
		default:
			if c < 0x20 {
				b = append(b, '\\', 'u', '0', '0', hexDigits[c>>4], hexDigits[c&0xf])
			} else {
				b = append(b, c)
			}
		}
	}
	return append(b, '"')
}

// Encode serializes protocol output with the canonical encoder (sorted keys,
// compact). Output values are produced by this program, so they are in domain.
func Encode(v any) []byte { return MustCanonical(v) }
