// Package sjson is the strict JSON parser and D-006 canonical serializer.
//
// Parsed values use plain Go types: nil, bool, int64, string, []any and
// map[string]any. Duplicate member names are rejected before any map is built,
// so a map never hides a first-wins or last-wins choice (D-017).
package sjson

import (
	"unicode/utf16"
	"unicode/utf8"
)

// Error codes (acceptance SEMANTICS, "Strict JSON").
const (
	ErrDuplicateKey     = "DUPLICATE_KEY"
	ErrNotInteger       = "NUMBER_NOT_INTEGER"
	ErrNegativeZero     = "NUMBER_NEGATIVE_ZERO"
	ErrOutOfRange       = "NUMBER_OUT_OF_RANGE"
	ErrInvalidJSON      = "INVALID_JSON"
	ErrLoneSurrogate    = "LONE_SURROGATE"
	ErrDepthExceeded    = "DEPTH_EXCEEDED"
	MaxDepth            = 64
	MaxSafeInteger      = int64(1)<<53 - 1
	maxSafeIntegerDigit = "9007199254740991"
)

// Error carries one of the codes above.
type Error struct{ Code string }

func (e *Error) Error() string { return e.Code }

type parser struct {
	s        string
	i        int
	maxDepth int
}

// Parse parses text with the strict rules and the D-006 depth limit.
func Parse(text string) (any, *Error) { return parse(text, MaxDepth) }

// ParseUnlimited parses with the strict rules but no depth limit. It is used
// only for the protocol envelope; the payload's depth is checked separately
// with CheckDepth so that the envelope does not shift the payload's depth.
func ParseUnlimited(text string) (any, *Error) { return parse(text, -1) }

func parse(text string, maxDepth int) (any, *Error) {
	if !utf8.ValidString(text) {
		return nil, &Error{ErrInvalidJSON}
	}
	p := &parser{s: text, maxDepth: maxDepth}
	p.ws()
	v, err := p.value(0)
	if err != nil {
		return nil, err
	}
	p.ws()
	if p.i != len(p.s) {
		return nil, &Error{ErrInvalidJSON}
	}
	return v, nil
}

func (p *parser) ws() {
	for p.i < len(p.s) {
		switch p.s[p.i] {
		case ' ', '\t', '\n', '\r':
			p.i++
		default:
			return
		}
	}
}

func (p *parser) depthOK(depth int) bool { return p.maxDepth < 0 || depth <= p.maxDepth }

func (p *parser) value(depth int) (any, *Error) {
	if !p.depthOK(depth) {
		return nil, &Error{ErrDepthExceeded}
	}
	if p.i >= len(p.s) {
		return nil, &Error{ErrInvalidJSON}
	}
	switch c := p.s[p.i]; {
	case c == '{':
		return p.object(depth)
	case c == '[':
		return p.array(depth)
	case c == '"':
		return p.str()
	case c == '-' || (c >= '0' && c <= '9'):
		return p.number()
	case p.lit("true"):
		return true, nil
	case p.lit("false"):
		return false, nil
	case p.lit("null"):
		return nil, nil
	}
	return nil, &Error{ErrInvalidJSON}
}

func (p *parser) lit(word string) bool {
	if len(p.s)-p.i >= len(word) && p.s[p.i:p.i+len(word)] == word {
		p.i += len(word)
		return true
	}
	return false
}

func (p *parser) object(depth int) (any, *Error) {
	p.i++ // '{'
	obj := map[string]any{}
	p.ws()
	if p.i < len(p.s) && p.s[p.i] == '}' {
		p.i++
		return obj, nil
	}
	for {
		p.ws()
		if p.i >= len(p.s) || p.s[p.i] != '"' {
			return nil, &Error{ErrInvalidJSON}
		}
		// Member names are at the parent's depth plus 1 (D-006, D-018 A1).
		if !p.depthOK(depth + 1) {
			return nil, &Error{ErrDepthExceeded}
		}
		key, err := p.str()
		if err != nil {
			return nil, err
		}
		if _, dup := obj[key.(string)]; dup {
			return nil, &Error{ErrDuplicateKey}
		}
		p.ws()
		if p.i >= len(p.s) || p.s[p.i] != ':' {
			return nil, &Error{ErrInvalidJSON}
		}
		p.i++
		p.ws()
		v, err := p.value(depth + 1)
		if err != nil {
			return nil, err
		}
		obj[key.(string)] = v
		p.ws()
		if p.i >= len(p.s) {
			return nil, &Error{ErrInvalidJSON}
		}
		switch p.s[p.i] {
		case ',':
			p.i++
		case '}':
			p.i++
			return obj, nil
		default:
			return nil, &Error{ErrInvalidJSON}
		}
	}
}

func (p *parser) array(depth int) (any, *Error) {
	p.i++ // '['
	arr := []any{}
	p.ws()
	if p.i < len(p.s) && p.s[p.i] == ']' {
		p.i++
		return arr, nil
	}
	for {
		p.ws()
		v, err := p.value(depth + 1)
		if err != nil {
			return nil, err
		}
		arr = append(arr, v)
		p.ws()
		if p.i >= len(p.s) {
			return nil, &Error{ErrInvalidJSON}
		}
		switch p.s[p.i] {
		case ',':
			p.i++
		case ']':
			p.i++
			return arr, nil
		default:
			return nil, &Error{ErrInvalidJSON}
		}
	}
}

func isDigit(c byte) bool { return c >= '0' && c <= '9' }

// number scans one token of the RFC 8259 number grammar, then applies the
// integer-only domain: fraction or exponent, then -0, then range.
func (p *parser) number() (any, *Error) {
	neg := false
	if p.s[p.i] == '-' {
		neg = true
		p.i++
	}
	if p.i >= len(p.s) || !isDigit(p.s[p.i]) {
		return nil, &Error{ErrInvalidJSON}
	}
	intStart := p.i
	if p.s[p.i] == '0' {
		p.i++
		// A leading zero followed by a digit (`01`, `-00`, `-01`) is not
		// JSON number syntax at all, so it is INVALID_JSON, never -0.
		if p.i < len(p.s) && isDigit(p.s[p.i]) {
			return nil, &Error{ErrInvalidJSON}
		}
	} else {
		for p.i < len(p.s) && isDigit(p.s[p.i]) {
			p.i++
		}
	}
	digits := p.s[intStart:p.i]
	nonInteger := false
	if p.i < len(p.s) && p.s[p.i] == '.' {
		p.i++
		if p.i >= len(p.s) || !isDigit(p.s[p.i]) {
			return nil, &Error{ErrInvalidJSON}
		}
		for p.i < len(p.s) && isDigit(p.s[p.i]) {
			p.i++
		}
		nonInteger = true
	}
	if p.i < len(p.s) && (p.s[p.i] == 'e' || p.s[p.i] == 'E') {
		p.i++
		if p.i < len(p.s) && (p.s[p.i] == '+' || p.s[p.i] == '-') {
			p.i++
		}
		if p.i >= len(p.s) || !isDigit(p.s[p.i]) {
			return nil, &Error{ErrInvalidJSON}
		}
		for p.i < len(p.s) && isDigit(p.s[p.i]) {
			p.i++
		}
		nonInteger = true
	}
	if nonInteger {
		return nil, &Error{ErrNotInteger}
	}
	if neg && digits == "0" {
		return nil, &Error{ErrNegativeZero}
	}
	if len(digits) > len(maxSafeIntegerDigit) ||
		(len(digits) == len(maxSafeIntegerDigit) && digits > maxSafeIntegerDigit) {
		return nil, &Error{ErrOutOfRange}
	}
	var n int64
	for i := 0; i < len(digits); i++ {
		n = n*10 + int64(digits[i]-'0')
	}
	if neg {
		n = -n
	}
	return n, nil
}

func hex4(s string) (rune, bool) {
	if len(s) < 4 {
		return 0, false
	}
	var r rune
	for i := 0; i < 4; i++ {
		c := s[i]
		var d byte
		switch {
		case c >= '0' && c <= '9':
			d = c - '0'
		case c >= 'a' && c <= 'f':
			d = c - 'a' + 10
		case c >= 'A' && c <= 'F':
			d = c - 'A' + 10
		default:
			return 0, false
		}
		r = r<<4 | rune(d)
	}
	return r, true
}

func (p *parser) str() (any, *Error) {
	p.i++ // opening quote
	var b []byte
	for {
		if p.i >= len(p.s) {
			return nil, &Error{ErrInvalidJSON}
		}
		c := p.s[p.i]
		switch {
		case c == '"':
			p.i++
			return string(b), nil
		case c < 0x20:
			return nil, &Error{ErrInvalidJSON}
		case c != '\\':
			b = append(b, c)
			p.i++
			continue
		}
		// escape
		p.i++
		if p.i >= len(p.s) {
			return nil, &Error{ErrInvalidJSON}
		}
		e := p.s[p.i]
		p.i++
		switch e {
		case '"', '\\', '/':
			b = append(b, e)
		case 'b':
			b = append(b, '\b')
		case 'f':
			b = append(b, '\f')
		case 'n':
			b = append(b, '\n')
		case 'r':
			b = append(b, '\r')
		case 't':
			b = append(b, '\t')
		case 'u':
			r, ok := hex4(p.s[p.i:])
			if !ok {
				return nil, &Error{ErrInvalidJSON}
			}
			p.i += 4
			switch {
			case r >= 0xDC00 && r <= 0xDFFF:
				return nil, &Error{ErrLoneSurrogate}
			case r >= 0xD800 && r <= 0xDBFF:
				if len(p.s)-p.i < 2 || p.s[p.i] != '\\' || p.s[p.i+1] != 'u' {
					return nil, &Error{ErrLoneSurrogate}
				}
				lo, ok := hex4(p.s[p.i+2:])
				if !ok {
					return nil, &Error{ErrInvalidJSON}
				}
				if lo < 0xDC00 || lo > 0xDFFF {
					return nil, &Error{ErrLoneSurrogate}
				}
				p.i += 6
				r = utf16.DecodeRune(r, lo)
			}
			b = utf8.AppendRune(b, r)
		default:
			return nil, &Error{ErrInvalidJSON}
		}
	}
}

// CheckDepth applies the D-006 traversal depth rule to an already parsed
// value whose root is at depth 0.
func CheckDepth(v any) *Error {
	if !depthWithin(v, 0) {
		return &Error{ErrDepthExceeded}
	}
	return nil
}

func depthWithin(v any, depth int) bool {
	if depth > MaxDepth {
		return false
	}
	switch t := v.(type) {
	case []any:
		for _, e := range t {
			if !depthWithin(e, depth+1) {
				return false
			}
		}
	case map[string]any:
		if len(t) > 0 && depth+1 > MaxDepth {
			return false
		}
		for _, e := range t {
			if !depthWithin(e, depth+1) {
				return false
			}
		}
	}
	return true
}
