//! The approval page's markup (M4-11; D-040 O-11; R6): escaped rendering of the broker's
//! stored, fingerprint-covered context, form decoding and the page script.
//!
//! Everything shown comes from the stored context (`ApprovalRequest::context`, whose
//! canonical digest is the fingerprint a challenge binds) plus the fingerprint itself and
//! the stored status and expiry; nothing from the request or another source is displayed.
//! Every text is untrusted and escaped for HTML text and attribute contexts; the page has
//! no inline script or style (the Content-Security-Policy forbids them).

use writ_broker::writ_kernel::json::Value;

/// Escape `text` for an HTML text or double-quoted attribute context.
pub fn escape(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for c in text.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#39;"),
            '/' => out.push_str("&#47;"),
            '`' => out.push_str("&#96;"),
            '=' => out.push_str("&#61;"),
            c if c.is_control() => out.push_str(&format!("&#{};", c as u32)),
            c => out.push(c),
        }
    }
    out
}

/// Escape `text` as the contents of a JSON string literal (no surrounding quotes).
pub fn json_string(text: &str) -> String {
    let mut out = String::with_capacity(text.len() + 2);
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '<' => out.push_str("\\u003c"),
            '>' => out.push_str("\\u003e"),
            '&' => out.push_str("\\u0026"),
            c if (c as u32) < 0x20 || c == '\u{2028}' || c == '\u{2029}' => {
                out.push_str(&format!("\\u{:04x}", c as u32))
            }
            c => out.push(c),
        }
    }
    out
}

/// Render a stored context as a nested definition list, every key and value escaped.
pub fn render_value(v: &Value) -> String {
    match v {
        Value::Null => "<span class=\"null\">null</span>".to_string(),
        Value::Bool(b) => format!("<span>{b}</span>"),
        Value::Int(i) => format!("<span>{i}</span>"),
        Value::Str(s) => format!("<span class=\"str\">{}</span>", escape(s)),
        Value::Arr(items) => {
            let mut out = String::from("<ol>");
            for item in items {
                out.push_str("<li>");
                out.push_str(&render_value(item));
                out.push_str("</li>");
            }
            out.push_str("</ol>");
            out
        }
        Value::Obj(members) => {
            let mut out = String::from("<dl>");
            for (k, item) in members {
                out.push_str("<dt>");
                out.push_str(&escape(k));
                out.push_str("</dt><dd>");
                out.push_str(&render_value(item));
                out.push_str("</dd>");
            }
            out.push_str("</dl>");
            out
        }
    }
}

/// What a request page shows: only stored, fingerprint-covered values (R6).
pub struct Displayed<'a> {
    /// The stored request id (the page's target).
    pub request_id: &'a str,
    /// The stored status text.
    pub status: &'a str,
    /// The stored expiry text.
    pub expires_at: &'a str,
    /// The context fingerprint (the digest a challenge binds).
    pub fingerprint: &'a str,
    /// The stored context, rendered in full.
    pub context: &'a Value,
    /// The page session's CSRF token.
    pub csrf: &'a str,
    /// Whether approve and deny are offered (only for a pending request).
    pub decidable: bool,
}

/// The request page.
pub fn request_page(d: &Displayed<'_>) -> String {
    let controls = if d.decidable {
        "<form id=\"decide\">\
         <label>Subject <input name=\"subject\" autocomplete=\"username webauthn\" required></label>\
         <button type=\"submit\" name=\"verdict\" value=\"approve\">Approve</button>\
         <button type=\"submit\" name=\"verdict\" value=\"deny\">Deny</button>\
         </form><p id=\"result\" role=\"status\"></p>"
    } else {
        "<p>This request is closed; nothing can be decided here.</p>"
    };
    format!(
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">\
         <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\
         <title>Writ approval</title><link rel=\"stylesheet\" href=\"/static/approval.css\">\
         <script src=\"/static/approval.js\" defer></script></head><body>\
         <main id=\"page\" data-request=\"{request}\" data-csrf=\"{csrf}\" data-fingerprint=\"{fp}\">\
         <h1>Approval request</h1>\
         <p>Everything below is the broker's stored record; descriptive text in it is \
         untrusted and was written by the requester.</p>\
         <table><tr><th>Request</th><td>{request}</td></tr>\
         <tr><th>Status</th><td>{status}</td></tr>\
         <tr><th>Expires</th><td>{expires}</td></tr>\
         <tr><th>Context fingerprint</th><td><code>{fp}</code></td></tr></table>\
         <h2>Stored context</h2><section class=\"context\">{context}</section>\
         {controls}</main></body></html>",
        request = escape(d.request_id),
        csrf = escape(d.csrf),
        fp = escape(d.fingerprint),
        status = escape(d.status),
        expires = escape(d.expires_at),
        context = render_value(d.context),
    )
}

/// A minimal page for a refusal (generic text only).
pub fn message_page(title: &str, text: &str) -> String {
    format!(
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>{t}</title>\
         <link rel=\"stylesheet\" href=\"/static/approval.css\"></head><body><main>\
         <h1>{t}</h1><p>{x}</p></main></body></html>",
        t = escape(title),
        x = escape(text),
    )
}

/// Decode an `application/x-www-form-urlencoded` body. `None` for malformed percent
/// escapes, invalid UTF-8 or a repeated field.
pub fn form(body: &[u8]) -> Option<Vec<(String, String)>> {
    let text = std::str::from_utf8(body).ok()?;
    let mut fields: Vec<(String, String)> = Vec::new();
    if text.is_empty() {
        return Some(fields);
    }
    for pair in text.split('&') {
        let (k, v) = pair.split_once('=').unwrap_or((pair, ""));
        let (k, v) = (decode_component(k)?, decode_component(v)?);
        if fields.iter().any(|(seen, _)| seen == &k) {
            return None;
        }
        fields.push((k, v));
    }
    Some(fields)
}

/// Decode one percent-encoded path segment (no `+` rule); `None` when malformed, empty or
/// containing a `/`.
pub fn path_segment(text: &str) -> Option<String> {
    if text.contains('+') {
        return None;
    }
    decode_component(text).filter(|s| !s.is_empty() && !s.contains('/'))
}

fn decode_component(text: &str) -> Option<String> {
    let bytes = text.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'+' => out.push(b' '),
            b'%' => {
                let hex = bytes.get(i + 1..i + 3)?;
                let hex = std::str::from_utf8(hex).ok()?;
                out.push(u8::from_str_radix(hex, 16).ok()?);
                i += 2;
            }
            b => out.push(b),
        }
        i += 1;
    }
    String::from_utf8(out).ok()
}

/// The page stylesheet.
pub const STYLE: &str = "body{font-family:system-ui,sans-serif;max-width:60rem;margin:1rem auto;\
padding:0 1rem}table{border-collapse:collapse}th,td{border:1px solid #888;padding:.25rem .5rem;\
text-align:left;vertical-align:top}dl{margin:0 0 0 1rem}dt{font-weight:bold}code,.str{\
white-space:pre-wrap;word-break:break-all}.null{color:#666}";

/// The page script: asks the broker for a challenge, runs the WebAuthn assertion with the
/// verifier's options (user verification required by the verifier) and posts the response.
/// It never chooses the operation's target or context: those are the server's.
pub const SCRIPT: &str = r#""use strict";
(function () {
  const page = document.getElementById("page");
  const form = document.getElementById("decide");
  if (!page || !form) { return; }
  const out = document.getElementById("result");
  const b64 = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const unb64 = (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0));
  const post = (path, fields) => fetch(path, {
    method: "POST", credentials: "same-origin", cache: "no-store",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(fields).toString(),
  }).then((r) => r.json());
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    out.textContent = "";
    try {
      const begun = await post("/ceremony/begin", {
        csrf: page.dataset.csrf, request_id: page.dataset.request,
        fingerprint: page.dataset.fingerprint, verdict: event.submitter.value,
        subject: form.elements.subject.value,
      });
      if (!begun.challenge_id) { out.textContent = begun.error || "refused"; return; }
      const pk = begun.options.publicKey;
      pk.challenge = unb64(pk.challenge);
      (pk.allowCredentials || []).forEach((c) => { c.id = unb64(c.id); });
      const cred = await navigator.credentials.get({ publicKey: pk });
      const r = cred.response;
      const response = JSON.stringify({
        id: cred.id, rawId: b64(cred.rawId), type: cred.type, extensions: {},
        response: {
          authenticatorData: b64(r.authenticatorData), clientDataJSON: b64(r.clientDataJSON),
          signature: b64(r.signature), userHandle: r.userHandle ? b64(r.userHandle) : null,
        },
      });
      const done = await post("/ceremony/finish", {
        csrf: page.dataset.csrf, challenge_id: begun.challenge_id, response: response,
      });
      out.textContent = done.result || done.error || "refused";
    } catch (e) {
      out.textContent = "The authenticator did not complete the ceremony.";
    }
  });
})();
"#;

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::*;
    use writ_broker::writ_kernel::json;

    #[test]
    fn untrusted_text_is_escaped_everywhere() {
        let hostile = "</dd><script>alert(1)</script>\"' onmouseover=x `";
        let v = json::obj([
            (hostile, json::s(hostile)),
            ("list", Value::Arr(vec![json::s(hostile)])),
        ]);
        let html = render_value(&v);
        assert!(!html.contains("<script"));
        assert!(!html.contains("</dd><s"), "{html}");
        assert!(!html.contains("\" onmouseover"));
        assert!(!html.contains("' onmouseover"));
        assert!(html.contains("&lt;&#47;dd&gt;&lt;script&gt;"));
        assert_eq!(escape("a\u{0}b"), "a&#0;b");
        assert_eq!(
            json_string("</script>\"\\\n"),
            "\\u003c/script\\u003e\\\"\\\\\\u000a"
        );
    }

    #[test]
    fn every_stored_member_is_displayed_including_reviewed_parties() {
        // A signoff context (X7) carries its reviewed parties; the page shows every member.
        let context = json::obj([
            ("context", json::s("writ.signoff-context.v1")),
            (
                "reviewed_parties",
                json::obj([
                    ("agent", json::s("agent-7")),
                    ("delegation_chain", json::s("unavailable")),
                    ("requester", json::s("u-<1234>")),
                ]),
            ),
        ]);
        let html = request_page(&Displayed {
            request_id: "s-1",
            status: "pending",
            expires_at: "2026-11-14T10:00:00Z",
            fingerprint: "sha256:abc",
            context: &context,
            csrf: "t",
            decidable: false,
        });
        for text in [
            "reviewed_parties",
            "agent-7",
            "delegation_chain",
            "unavailable",
            "u-&lt;1234&gt;",
        ] {
            assert!(html.contains(text), "{text}");
        }
        assert!(
            !html.contains("<form"),
            "a closed target offers no decision"
        );
    }

    #[test]
    fn forms_decode_strictly() {
        assert_eq!(
            form(b"a=1+2&b=%2Fx%3D").unwrap(),
            [("a".into(), "1 2".into()), ("b".into(), "/x=".into())]
        );
        assert_eq!(form(b"a=%zz"), None);
        assert_eq!(form(b"a=%2"), None);
        assert_eq!(form(b"a=1&a=2"), None);
        assert_eq!(form(b"a=%ff"), None);
        assert_eq!(form(b"").unwrap(), []);
    }
}
