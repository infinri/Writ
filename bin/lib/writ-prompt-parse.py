"""Prompt parser + mode-hint classifier for writ-rag-inject.sh (UserPromptSubmit).

Extracted VERBATIM from the hook's inline `python3 -c` block (was lines 86-206).
Reads the Claude Code envelope JSON on stdin; prints 4 newline-separated fields:
  session_id\nagent_id\nmode_hint\nprompt  (or 4 empty lines on any error).
The PROMPT IS LAST because it is the only field that may legitimately contain the
delimiter; the consumer reads it as the remainder of the record, so a multi-line prompt
arrives whole instead of truncating and shifting every field after it.
Lives in bin/lib next to writ_mode_hint.py, so its own dir resolves the classifier
import with no $WRIT_DIR interpolation. stdlib-only; fail-open."""
import os, sys, json, re

MAX_KEYWORDS = 25

# Common English stopwords + conversational filler
STOPWORDS = frozenset(
    'a an the is are was were be been being have has had do does did will would '
    'shall should may might can could of in to for on with at by from as into '
    'through during before after above below between out off over under again '
    'further then once here there when where why how all each every both few '
    'more most other some such no nor not only own same so than too very just '
    'also about up its it i me my we our you your he him his she her they them '
    'their what which who whom this that these those am let get got if but and '
    'or because until while although since even though however still yet already '
    'please dont im ive weve youre theyre doesnt didnt wont cant isnt arent '
    'seems like think want need know see look make sure something anything '
    'everything nothing really actually probably maybe already going doing '
    'using used way things stuff lot much many well right now here there '
    'also another first last next new old good bad big small long short give '
    'take come go say tell ask try keep start stop run work help show move '
    'yes no ok okay hey hi hello thanks thank sorry'.split()
)

def extract_keywords(raw: str) -> str:
    # Strip fenced code blocks but keep language hints.
    langs = re.findall(r'\x60\x60\x60(\w+)', raw)
    text = re.sub(r'\x60\x60\x60[\s\S]*?\x60\x60\x60', ' ', raw)
    # Strip inline code spans.
    text = re.sub(r'\x60[^\x60]+\x60', ' ', text)
    # Strip markdown/table/tool chrome.
    text = re.sub(r'[│┌┐└┘├┤┬┴┼─━┃╌╍╎╏═║╔╗╚╝╠╣╦╩╬|]', ' ', text)
    text = re.sub(r'●[^\n]*', ' ', text)
    text = re.sub(r'⎿.*', ' ', text)
    text = re.sub(r'[✻◆▐▛▜▌▝▘]+[^\n]*', ' ', text)
    # Strip URLs
    text = re.sub(r'https?://\S+', ' ', text)
    # Strip non-alphanumeric except hyphens and underscores (preserve technical terms)
    text = re.sub(r'[^a-zA-Z0-9_\-/.\s]', ' ', text)
    # Tokenize
    words = text.split()
    # Filter: keep technical terms, remove stopwords and short noise
    keywords = []
    seen = set()
    for w in words:
        lower = w.lower().strip('.-/')
        if not lower or len(lower) < 3:
            continue
        if lower in STOPWORDS:
            continue
        if lower in seen:
            continue
        seen.add(lower)
        # Prefer: capitalized words, words with underscores/hyphens, file-like patterns
        keywords.append(w if (w[0].isupper() or '_' in w or '-' in w or '.' in w) else lower)
    # Add language hints from code fences
    for lang in set(langs):
        if lang.lower() not in seen:
            keywords.append(lang)
            seen.add(lang.lower())
    # Cap and join
    return ' '.join(keywords[:MAX_KEYWORDS])

def _one_line(v, _flat=str.maketrans('\r\n', '  ')):
    # Delimiter-freedom for the three scalars, enforced at the PRODUCER rather than assumed
    # at the consumer: a pathological value is mangled inside its OWN field and can never
    # move another. Mangled, not truncated, because a truncated session id could collide
    # with a real session while a mangled one simply matches nothing. str() keeps a
    # non-string envelope value (an int agent_id) out of the exception arm. The table is a
    # default arg so a call site is the only place this name is followed by an argument,
    # which is what makes "delete the sanitizer" a runnable mutation in the suite.
    return str(v).translate(_flat)

try:
    data = json.load(sys.stdin)
    sid = data.get('agent_id', '') or data.get('session_id', '')
    agent_id = data.get('agent_id', '')
    raw = data.get('prompt', data.get('message', data.get('content', '')))
    prompt = extract_keywords(raw) if len(raw) > 300 else raw
    # Mode auto-routing: classify the RAW prompt (the keyword-extracted form scrambles the
    # phrases the classifier needs) for an audit/explore/research shape. Guarded so a
    # classifier failure never breaks prompt parsing -- emit empty hint on any error.
    hint = ''
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from writ_mode_hint import classify_mode_hint  # standalone, stdlib-only (load-robust)
        hint = classify_mode_hint(raw) or ''
    except Exception:
        hint = ''
    # Transcript classification (recall): when the single prompt does not classify, the recent
    # conversation often does (e.g. 'ok go ahead' / 'now build it' after a planning exchange).
    # Read only the TAIL of the transcript, take the last few USER text messages, re-classify.
    # Bounded (64KB tail) + fully guarded so the per-prompt hot path stays cheap and never breaks.
    if not hint:
        try:
            tp = data.get('transcript_path', '')
            if tp:
                with open(tp, 'rb') as f:
                    f.seek(0, 2)
                    f.seek(max(0, f.tell() - 65536))
                    tail = f.read().decode('utf-8', 'ignore')
                users = []
                for line in tail.splitlines():
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    if ev.get('type') != 'user':
                        continue
                    c = (ev.get('message') or {}).get('content')
                    if isinstance(c, str):
                        users.append(c)
                    elif isinstance(c, list):
                        users += [it.get('text', '') for it in c
                                  if isinstance(it, dict) and it.get('type') == 'text']
                recent = ' '.join(u for u in users[-5:] if u)
                if recent:
                    hint = classify_mode_hint(recent) or ''
        except Exception:
            pass
    # permission_mode is a high-precision native CC signal: 'plan' = the user is in CC plan
    # mode (about to implement) -> work. Upgrades an empty/weak keyword hint; never overrides
    # an investigate classification (audit-while-planning stays the gate-light investigate).
    if data.get('permission_mode', '') == 'plan' and hint != 'investigate':
        hint = 'work'
    sid = _one_line(sid)
    agent_id = _one_line(agent_id)
    hint = _one_line(hint)
    print(f'{sid}\n{agent_id}\n{hint}\n{prompt}')
except Exception as e:
    print('\n\n\n')
