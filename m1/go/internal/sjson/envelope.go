package sjson

import "unicode/utf8"

// ReadID recovers the `id` of a protocol envelope line that failed strict
// parsing or validation, so the line can still be answered ENVELOPE_INVALID.
// It scans only the top-level object: member names are decoded strictly,
// other member values are skipped tolerantly. The id is readable only when
// exactly one top-level member is named "id" (after escape decoding) and its
// value is a well-formed string. It never builds or trusts the payload.
func ReadID(text string) (string, bool) {
	p := &parser{s: text, maxDepth: -1}
	p.ws()
	if p.i >= len(p.s) || p.s[p.i] != '{' {
		return "", false
	}
	p.i++
	id, ids, idOK := "", 0, false
	for {
		p.ws()
		if p.i >= len(p.s) || p.s[p.i] != '"' {
			break
		}
		k, err := p.str()
		if err != nil {
			break
		}
		p.ws()
		if p.i >= len(p.s) || p.s[p.i] != ':' {
			break
		}
		p.i++
		p.ws()
		if k.(string) == "id" {
			ids++
			idOK = false
			if p.i < len(p.s) && p.s[p.i] == '"' {
				if v, err := p.str(); err == nil && utf8.ValidString(v.(string)) {
					id, idOK = v.(string), true
				}
			}
		}
		if !p.skipLenient() {
			break
		}
	}
	return id, ids == 1 && idOK
}

// skipLenient advances past the rest of a member value up to and including
// the next top-level ','; it returns false at the end of the object or text.
func (p *parser) skipLenient() bool {
	depth := 0
	for p.i < len(p.s) {
		c := p.s[p.i]
		p.i++
		switch c {
		case '"':
			for p.i < len(p.s) && p.s[p.i] != '"' {
				if p.s[p.i] == '\\' {
					p.i++
				}
				p.i++
			}
			p.i++
		case '{', '[':
			depth++
		case '}', ']':
			if depth == 0 {
				return false
			}
			depth--
		case ',':
			if depth == 0 {
				return true
			}
		}
	}
	return false
}
