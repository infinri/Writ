package sjson

// Small typed accessors over parsed values. They never panic.

// Obj returns v as an object.
func Obj(v any) (map[string]any, bool) {
	m, ok := v.(map[string]any)
	return m, ok
}

// Arr returns v as an array.
func Arr(v any) ([]any, bool) {
	a, ok := v.([]any)
	return a, ok
}

// Str returns m[k] as a string.
func Str(m map[string]any, k string) (string, bool) {
	if m == nil {
		return "", false
	}
	s, ok := m[k].(string)
	return s, ok
}

// StrOr returns m[k] as a string or "".
func StrOr(m map[string]any, k string) string {
	s, _ := Str(m, k)
	return s
}

// Int returns m[k] as an integer.
func Int(m map[string]any, k string) (int64, bool) {
	if m == nil {
		return 0, false
	}
	n, ok := m[k].(int64)
	return n, ok
}

// Has reports whether k is a member of m (null counts as present).
func Has(m map[string]any, k string) bool {
	if m == nil {
		return false
	}
	_, ok := m[k]
	return ok
}

// Strings returns v as an array of strings.
func Strings(v any) ([]string, bool) {
	a, ok := v.([]any)
	if !ok {
		return nil, false
	}
	out := make([]string, 0, len(a))
	for _, e := range a {
		s, ok := e.(string)
		if !ok {
			return nil, false
		}
		out = append(out, s)
	}
	return out, true
}

// Clone deep-copies a parsed value.
func Clone(v any) any {
	switch t := v.(type) {
	case []any:
		out := make([]any, len(t))
		for i, e := range t {
			out[i] = Clone(e)
		}
		return out
	case map[string]any:
		out := make(map[string]any, len(t))
		for k, e := range t {
			out[k] = Clone(e)
		}
		return out
	}
	return v
}

// Without returns a shallow copy of m without the named members.
func Without(m map[string]any, keys ...string) map[string]any {
	out := make(map[string]any, len(m))
	for k, v := range m {
		out[k] = v
	}
	for _, k := range keys {
		delete(out, k)
	}
	return out
}
