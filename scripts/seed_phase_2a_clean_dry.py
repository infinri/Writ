"""Phase 2A of the public rulebook expansion: Clean Code + DRY.

Seeds 27 new CLEAN-* and DRY-* rules into Neo4j (0 mandatory) and renames
6 legacy ARCH-* rules to align with the public-rulebook IDs:

  ARCH-FUNC-001  -> CLEAN-FUNC-002  (function length cap)
  ARCH-CONST-001 -> CLEAN-MAGIC-001 (no magic literals)
  ARCH-ERR-001   -> CLEAN-ERR-001   (broadened: empty catches forbidden)
  ARCH-DRY-001   -> DRY-DUP-001     (duplication threshold)
  ARCH-SSOT-001  -> DRY-CONFIG-001  (single source of truth for config)

ARCH-COMP-001 (inheritance depth <= 2) is kept as a Writ-specific extension
of CLEAN-NEST-001 (it covers class hierarchies rather than control-flow nesting).

Idempotent. Re-runs MERGE existing rules with the same rule_id.

Per the public rulebook source: out-of-the-box-rules.md sections 2, 3.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _seed_helpers import connect, upsert_rule

TODAY = date.today().isoformat()


def _rule(
    rid: str,
    domain: str,
    severity: str,
    scope: str,
    trigger: str,
    statement: str,
    violation: str,
    pass_example: str,
    enforcement: str,
    rationale: str,
    source_section: str,
) -> dict:
    return {
        "rule_id": rid,
        "domain": domain,
        "severity": severity,
        "scope": scope,
        "trigger": trigger,
        "statement": statement,
        "violation": violation,
        "pass_example": pass_example,
        "enforcement": enforcement,
        "rationale": rationale,
        "mandatory": False,
        "mechanical_enforcement_path": None,
        "confidence": "production-validated",
        "authority": "human",
        "times_seen_positive": 0,
        "times_seen_negative": 0,
        "last_validated": TODAY,
        "evidence": "doc:public-rulebook-2026-05",
        "staleness_window": 365,
        "always_on": False,
        "body": "",
        "source_attribution": f"out-of-the-box-rules.md section {source_section}",
        "source_commit": "",
    }


# ============================================================================
# Clean Code (25 rules, including 3 renames preserved with broader phrasing)
# ============================================================================
CLEAN_RULES = [
    _rule("CLEAN-NAME-001", "code-quality", "medium", "component",
        "When naming a function, variable, class, type, constant, module, or file.",
        "Identifiers use descriptive, domain-meaningful names. Single-letter names are reserved for loop counters (i, j, k) and short lambda parameters. Abbreviations are reserved for established conventions (id, url, http).",
        "```python\ndef p(x, y):\n    return x * 1.08 if y else x\n```",
        "```python\ndef apply_tax(amount, taxable):\n    return amount * TAX_RATE if taxable else amount\n```",
        "Code review.",
        "Names are the readers' shortest path to intent. Cryptic names force re-deriving meaning from context every time the code is read.",
        "2"),
    _rule("CLEAN-NAME-002", "code-quality", "medium", "component",
        "When naming a boolean variable, function returning bool, or property of bool type.",
        "Boolean names start with is/has/can/should/needs or read as a predicate (active, enabled). Names that imply nouns (`status`, `flag`) for booleans are violations.",
        "```python\nstatus = user_paid_invoice  # bool\nflag = True\n```",
        "```python\nis_invoice_paid = user_paid_invoice\nhas_active_subscription = subscription.active\n```",
        "Code review.",
        "Predicate names let the reader scan the condition as English. `if user.active:` reads correctly; `if user.status:` could mean anything.",
        "2"),
    _rule("CLEAN-FUNC-001", "code-quality", "high", "component",
        "When writing a function that both returns a value and changes state.",
        "Functions do one thing. Command-Query Separation: a function either returns a computed value (query) or mutates state (command), never both. Functions that mutate-and-return are split.",
        "```python\ndef get_order(id):\n    order = Order.fetch(id)\n    order.viewed_at = now()\n    order.save()\n    return order\n```",
        "```python\ndef get_order(id):\n    return Order.fetch(id)\n\ndef mark_order_viewed(order):\n    order.viewed_at = now()\n    order.save()\n```",
        "Code review.",
        "CQS makes side effects visible at the call site. A 'getter' that mutates is a landmine: every caller now has hidden persistence semantics it must reason about.",
        "2"),
    _rule("CLEAN-FUNC-002", "code-quality", "medium", "component",
        "When writing or modifying any function or method.",
        "Functions stay at or below 50 lines of logic (excluding declarations, comments, and blank lines). Longer functions are decomposed into named sub-functions that read as a top-down narrative.",
        "```python\ndef process_order(order):\n    # 180 lines of validation, pricing, payment, fulfillment, email,\n    # audit log, analytics, inventory reservation, ...\n    ...\n```",
        "```python\ndef process_order(order):\n    validate(order)\n    price = price_order(order)\n    payment = charge_customer(order.customer, price)\n    fulfill(order)\n    notify_customer(order)\n```",
        "Linter rules (eslint complexity/max-lines-per-function, pylint too-many-statements). Code review.",
        "Long functions hide the shape of the work. Decomposed code reads like a table of contents at the entry point and lets the reader drill in only where needed.",
        "2"),
    _rule("CLEAN-FUNC-003", "code-quality", "medium", "component",
        "When defining or modifying a function signature.",
        "Functions accept at most 4 positional parameters. Beyond that, an options/config object (Pydantic model, dataclass, dict) is passed. Long argument lists indicate the function is doing too much or that several arguments belong together as a named concept.",
        "```python\ndef ship_order(order_id, address_line1, address_line2, city, state, zip, country, carrier, service_level, signature_required):\n    ...\n```",
        "```python\n@dataclass\nclass ShippingAddress:\n    line1: str; line2: str | None; city: str; state: str; zip: str; country: str\n\ndef ship_order(order_id, address: ShippingAddress, options: ShippingOptions):\n    ...\n```",
        "Code review.",
        "Long parameter lists are positional-coupling hazards: callers easily mis-order arguments, and adding a new parameter ripples through every call site.",
        "2"),
    _rule("CLEAN-FUNC-004", "code-quality", "medium", "component",
        "When adding a boolean parameter that switches function behavior.",
        "Flag arguments that switch function behavior are forbidden. Split the function into two named functions, one per branch. Distinct behavior belongs to distinct names.",
        "```python\ndef render_user(user, is_public=True):\n    if is_public:\n        return public_view(user)\n    return private_view(user)\n```",
        "```python\ndef render_user_public(user):\n    return public_view(user)\n\ndef render_user_private(user):\n    return private_view(user)\n```",
        "Code review.",
        "Flag arguments hide that the function is two functions in disguise. Named alternatives make the choice explicit at the call site.",
        "2"),
    _rule("CLEAN-NEST-001", "code-quality", "high", "component",
        "When the body of a function or method exceeds 3 levels of nesting (loops, conditionals, try/with).",
        "Maximum nesting depth is 3 levels. Deeper code is extracted via early returns, guard clauses, or helper functions. Pyramids of doom are violations.",
        "```python\ndef process(item):\n    if item:\n        if item.active:\n            for child in item.children:\n                if child.valid:\n                    if child.amount > 0:\n                        do_work(child)\n```",
        "```python\ndef process(item):\n    if not item or not item.active:\n        return\n    for child in item.children:\n        process_child(child)\n\ndef process_child(child):\n    if not child.valid or child.amount <= 0:\n        return\n    do_work(child)\n```",
        "Linter rules (eslint complexity/max-depth, pylint too-many-nested-blocks). Code review.",
        "Deeply nested code forces the reader to track too many simultaneous conditions. Early-returns flatten the structure into the happy path.",
        "2"),
    _rule("CLEAN-COMMENT-001", "code-quality", "medium", "component",
        "When writing inline comments adjacent to code.",
        "Comments explain WHY the code is the way it is (constraint, hidden invariant, surprising business rule, workaround for a known bug). Comments that restate WHAT the next line does in natural language are deleted or rewritten to add intent.",
        "```python\n# Add tax to total\ntotal = subtotal + (subtotal * tax_rate)\n```",
        "```python\n# Tax computed on subtotal, not on shipping. Per AR-203: shipping is\n# already tax-inclusive from the carrier quote.\ntotal = subtotal + (subtotal * tax_rate)\n```",
        "Code review.",
        "Restating-the-code comments rot the moment the code changes. WHY-comments stay useful because the constraint outlives the syntax.",
        "2"),
    _rule("CLEAN-COMMENT-002", "code-quality", "medium", "component",
        "When opening a pull request or committing.",
        "Commented-out code is never committed. Version control is the history; comments are not. Removed code is removed entirely; if it might be needed later, that future-self can recover it from git.",
        "```python\n# old logic, leaving in case we revert:\n# total = subtotal * 1.08\ntotal = subtotal * tax_rate\n```",
        "```python\ntotal = subtotal * tax_rate\n```",
        "Pre-commit hook can flag large commented-out blocks. Code review.",
        "Commented-out code is dead weight that confuses readers and bloats diffs. Git remembers; the file does not need to.",
        "2"),
    _rule("CLEAN-DEAD-001", "code-quality", "medium", "component",
        "When committing changes that leave dead code behind: statements after a return, raise, break or continue that can never run, unused imports, unused variables, unused functions, or branches that are never taken.",
        "No dead code in committed code: no unreachable statements (code after a return, raise, break or continue, or under a condition that is always false), no unused imports, no unused variables, and no unused functions. The linter is configured to flag these as errors.",
        "```python\nimport os, sys  # sys never referenced\n\ndef compute(x):\n    y = x * 2  # never used\n    return x + 1\n```",
        "```python\nimport os\n\ndef compute(x):\n    return x + 1\n```",
        "Linter rules (ruff F401/F841, eslint no-unused-vars/no-unreachable, gosec deadcode). CI gate.",
        "Dead code distracts readers and rots over time. Removing it is free.",
        "2"),
    _rule("CLEAN-MAGIC-001", "code-quality", "high", "component",
        "When using a numeric or string literal in code that has business meaning beyond its raw value.",
        "Magic literals are extracted to named constants with domain context. Numbers like 86400, 1000, 3, strings like 'admin', 'success' are replaced with named constants. Tax rates, fee thresholds, retry counts, status codes all carry names.",
        "```python\nif elapsed > 86400:  # what does 86400 mean?\n    archive(record)\n```",
        "```python\nONE_DAY_SECONDS = 86400  # archive policy: records >= 1 day old\nif elapsed > ONE_DAY_SECONDS:\n    archive(record)\n```",
        "Linter rules. Code review.",
        "A named constant explains the value at the use site and at its definition. Magic literals force the reader to derive the meaning by triangulating with adjacent code.",
        "2"),
    _rule("CLEAN-RETURN-001", "code-quality", "medium", "component",
        "When a function hands back values of different types on different branches, or signals failure with a sentinel value such as False, -1 or an empty string instead of None, an optional type or a result type.",
        "Functions return a single, predictable type. A function whose return type varies (string-or-None-or-list) is split or refactored to return an explicit result/optional type. Sentinel return values (`return False on error, return the_value on success`) are forbidden.",
        "```python\ndef find_user(id):\n    user = db.get(id)\n    if not user:\n        return False  # sentinel; caller must distinguish from user=0\n    return user\n```",
        "```python\ndef find_user(id) -> User | None:\n    return db.get(id)\n# or, with a Result type:\ndef find_user(id) -> Result[User, NotFoundError]:\n    ...\n```",
        "Type checker (mypy strict, pyright). Code review.",
        "Multiple return types push the caller into ad-hoc type checks at every call site. Single-type returns let the type system carry the contract.",
        "2"),
    _rule("CLEAN-SIDE-001", "code-quality", "high", "component",
        "When writing or naming a function.",
        "Functions named as getters, computers, builders, or formatters (`get_*`, `compute_*`, `build_*`, `to_*`, `as_*`, `format_*`) have no side effects: no I/O, no mutation of arguments, no state changes. Naming and behavior agree.",
        "```python\ndef get_total(order):\n    order.viewed_at = now()  # mutates argument\n    db.session.commit()      # I/O\n    return order.amount\n```",
        "```python\ndef get_total(order):\n    return order.amount\n```",
        "Code review.",
        "When name and behavior disagree, the caller's mental model is wrong. Side-effect-free getters are safe to call anywhere; side-effecting getters create implicit dependencies.",
        "2"),
    _rule("CLEAN-ERR-001", "code-quality", "high", "component",
        "When writing try/except, try/catch, try/rescue, or any exception-handling block.",
        "No empty catch blocks. Caught exceptions are either re-raised (preserving cause), logged with full context, or converted to a domain-specific error type. Swallowing exceptions silently is forbidden.",
        "```python\ntry:\n    charge_card(order)\nexcept Exception:\n    pass  # whatever, move on\n```",
        "```python\ntry:\n    charge_card(order)\nexcept StripeError as e:\n    logger.exception('charge failed', extra={'order_id': order.id})\n    raise PaymentFailed(order.id) from e\n```",
        "Linter rules (eslint no-empty, ruff S110, pylint bare-except, PHPCS Generic.CodeAnalysis.EmptyStatement).",
        "Silent exception swallowing turns a stack-trace-in-monitoring into a mystery failure that surfaces minutes or hours later. Logging or re-raising preserves the signal.",
        "2"),
    _rule("CLEAN-ERR-002", "code-quality", "high", "component",
        "When writing exception-handling blocks.",
        "Exception types caught are specific. `except Exception`, `catch(Exception)`, `rescue` without a class are forbidden except at top-level boundary handlers (uncaught-error middleware, job runner). Specific catches signal which conditions the code is prepared to handle.",
        "```python\ntry:\n    user = User.query.get(id)\nexcept Exception:  # masks programming errors as 'user not found'\n    return None\n```",
        "```python\ntry:\n    user = User.query.get(id)\nexcept NoResultFound:\n    return None\n```",
        "Linter rules.",
        "Bare `except` catches AttributeError, KeyError, NameError, KeyboardInterrupt -- everything. Specific catches keep programming bugs visible.",
        "2"),
    _rule("CLEAN-ERR-003", "code-quality", "medium", "component",
        "When raising or constructing exception/error objects.",
        "Error messages include context: what was being attempted, the input value (when safe to log), and any relevant identifier. Bare messages like 'Invalid' or 'Failed' are violations.",
        "```python\nraise ValueError('Invalid')\n```",
        "```python\nraise ValueError(f'Invalid order status \"{status}\" for order {order_id}: expected one of {VALID_STATUSES}')\n```",
        "Code review.",
        "Context-rich errors are the on-call engineer's first line of defense. Bare messages force a code dive to figure out what went wrong.",
        "2"),
    _rule("CLEAN-ASSERT-001", "code-quality", "medium", "component",
        "When using `assert` statements (or equivalent: console.assert, debug_assert).",
        "Assertions are reserved for invariants -- conditions that the developer believes can never be false. Assertions are not for input validation, control flow, or error handling. Production builds may strip assertions; security-critical or business-critical checks must use explicit raise/throw.",
        "```python\ndef transfer(amount, balance):\n    assert amount <= balance, 'overdraft'  # user can cause this!\n    debit(amount)\n```",
        "```python\ndef transfer(amount, balance):\n    if amount > balance:\n        raise InsufficientFundsError(balance, amount)\n    debit(amount)\n    # Invariant after debit -- assertion-appropriate:\n    assert new_balance(balance, amount) >= 0\n```",
        "Code review.",
        "Stripped assertions in production silently let the bad path proceed. Real validation belongs in real checks; assertions are for developer-facing 'this should be impossible' guards.",
        "2"),
    _rule("CLEAN-COUPLING-001", "code-quality", "high", "component",
        "When importing or constructing dependencies inside a module.",
        "Modules depend on abstractions (interfaces, protocols, base classes), not concrete implementations. Direct construction of concrete classes inside business logic is a violation; dependencies are injected.",
        "```python\nclass OrderService:\n    def __init__(self):\n        self.stripe = StripeClient()  # hard-coded concrete\n```",
        "```python\nclass OrderService:\n    def __init__(self, payments: PaymentGateway):\n        self.payments = payments\n```",
        "Code review.",
        "Hard-coded dependencies make code untestable and untransferable. Abstractions let the module work against any conforming implementation.",
        "2"),
    _rule("CLEAN-COUPLING-002", "code-quality", "medium", "component",
        "When organizing module dependencies.",
        "Circular imports or circular module dependencies are forbidden. A reaches B and B reaches A indicates the responsibilities are tangled; extract the shared concept to a third module or invert the dependency.",
        "```python\n# users.py imports orders.py; orders.py imports users.py\n```",
        "```python\n# users.py, orders.py both import shared/types.py\n# orders.py imports users.py (one direction only)\n```",
        "Linter rules (ruff cyclic-import, eslint import/no-cycle).",
        "Cycles make the dependency graph un-orderable and the modules un-testable in isolation.",
        "2"),
    _rule("CLEAN-FORMAT-001", "code-quality", "low", "component",
        "When committing or reviewing source code.",
        "Formatting is enforced by a configured formatter (Prettier, Black, rustfmt, gofmt, ktfmt). Style-only review comments are forbidden; the formatter wins. Disagreements about formatting are settled by changing the formatter config, not by per-PR debate.",
        "```\n# PR review: 'add a space after the comma here'\n```",
        "```\n# pre-commit: prettier --write . / black . / rustfmt **/*.rs\n```",
        "Pre-commit hook. CI gate.",
        "Formatter wars consume engineering time without product value. Delegate the entire decision to a tool and move on.",
        "2"),
    _rule("CLEAN-TODO-001", "code-quality", "low", "component",
        "When writing a TODO, FIXME, HACK, or XXX comment.",
        "TODO comments include a tracking reference (issue/ticket number) or an author + date. Bare 'TODO' without context becomes orphaned forever.",
        "```python\n# TODO: handle null case\n```",
        "```python\n# TODO(ORD-1421): handle the null-billing-address case after the\n# customer-import refactor lands.\n```",
        "Linter rule, code review.",
        "Untracked TODOs accumulate into a graveyard that no one cleans. Tracking forces the comment to either become work or be deleted.",
        "2"),
    _rule("CLEAN-LOG-001", "code-quality", "medium", "component",
        "When emitting log output from application code.",
        "Logs use structured logging (JSON or key-value) with severity levels. `print()`, `System.out.println`, `console.log` for application logs are violations. Use the configured logger with appropriate level.",
        "```python\nprint(f'user {user_id} logged in')\n```",
        "```python\nlogger.info('user logged in', extra={'user_id': user_id})\n```",
        "Linter rule (ruff T201 for `print`, eslint no-console). Code review.",
        "Structured logs are queryable, filterable, and routable. print()-style logs require regex parsing and lose context when handlers change.",
        "2"),
    _rule("CLEAN-LOG-002", "code-quality", "medium", "component",
        "When emitting logs from request-handling code.",
        "Log messages include a correlation/request ID propagated from the inbound request. The ID is set at the entry point (middleware) and attached to every log line for the request, including downstream calls.",
        "```python\nlogger.info('processing order', extra={'order_id': order.id})\n# no correlation_id; impossible to join with other services\n```",
        "```python\nlogger.info('processing order', extra={\n    'order_id': order.id,\n    'correlation_id': request.headers.get('X-Correlation-ID'),\n})\n```",
        "Middleware that injects the correlation ID into the logger's contextvars. Code review.",
        "Distributed systems are only debuggable when logs across services share an identifier. The correlation ID is the join key for an incident.",
        "2"),
    _rule("CLEAN-BOOL-001", "code-quality", "medium", "component",
        "When writing conditional expressions.",
        "Boolean comparisons to True/False literal are forbidden. Use the value directly: `if active:` not `if active == True:`. Negation: `if not active:` not `if active == False:`.",
        "```python\nif user.is_admin == True and user.is_active == True:\n    grant()\n```",
        "```python\nif user.is_admin and user.is_active:\n    grant()\n```",
        "Linter rules (pylint comparison-with-callable / eqeq, eslint eqeqeq).",
        "Explicit comparison with a literal adds noise without adding precision. The truthy/falsy interpretation is the convention.",
        "2"),
    _rule("CLEAN-TERNARY-001", "code-quality", "low", "component",
        "When writing conditional expressions.",
        "Nested ternaries are forbidden. `a if c else (b if d else e)` is rewritten as an if/elif/else block. Single-level ternary is acceptable when both branches are short.",
        "```python\nresult = ('admin' if user.is_admin else\n          ('staff' if user.is_staff else\n          ('vip' if user.is_vip else 'user')))\n```",
        "```python\nif user.is_admin:\n    result = 'admin'\nelif user.is_staff:\n    result = 'staff'\nelif user.is_vip:\n    result = 'vip'\nelse:\n    result = 'user'\n```",
        "Code review.",
        "Nested ternaries collapse intent into a riddle. Block form is verbose but legible.",
        "2"),
]


# ============================================================================
# DRY (8 rules; 2 renames preserved)
# ============================================================================
DRY_RULES = [
    _rule("DRY-DUP-001", "code-quality", "high", "component",
        "When the same logic block appears in two or more locations.",
        "Duplicated logic of 5+ substantively identical lines must be extracted to a shared function or module. Cosmetic differences (variable names, formatting) do not exempt duplication. The exemption for one-off scripts is narrow and documented.",
        "```python\n# orders.py\ntax = subtotal * 0.08\ntotal = subtotal + tax\n\n# invoices.py\ntax = invoice.amount * 0.08\ntotal = invoice.amount + tax\n```",
        "```python\n# pricing.py\ndef apply_tax(amount, rate=TAX_RATE):\n    return amount + (amount * rate)\n\n# orders.py / invoices.py both import apply_tax\n```",
        "Code review. Duplicate-detection tools (jscpd, pylint duplicate-code).",
        "Duplication multiplies bug surface: a fix in one copy leaves the others wrong. Extraction is the structural defense.",
        "3"),
    _rule("DRY-DUP-002", "code-quality", "medium", "component",
        "When the same constant value (number, string, regex) appears in multiple files.",
        "Constants used in 2+ locations are centralized in a constants/config module and imported. Repeating a tax rate, a timeout, a status string, an API base URL across files is a violation.",
        "```python\n# A.py: TAX_RATE = 0.08\n# B.py: tax = amount * 0.08\n# C.py: 'TAX_RATE: 8%' in a UI string\n```",
        "```python\n# constants.py: TAX_RATE = 0.08\n# all callers: from constants import TAX_RATE\n```",
        "Code review. Linter rules can flag literal magic numbers (overlaps with CLEAN-MAGIC-001).",
        "Scattered constants drift the moment business needs change. One canonical definition makes a single edit the only edit.",
        "3"),
    _rule("DRY-DUP-003", "code-quality", "medium", "component",
        "When writing input validation logic.",
        "Validation logic is shared via schemas (Pydantic, Zod, JSON Schema) or validator functions, not copy-pasted across handlers. A given field's rules live in one place.",
        "```python\n# Each endpoint re-implements email regex check and length check.\n```",
        "```python\nclass EmailInput(BaseModel):\n    email: EmailStr\n# All endpoints accept EmailInput.\n```",
        "Code review.",
        "Validation drift across endpoints leads to one endpoint accepting input another rejects. Shared schemas keep the contract uniform.",
        "3"),
    _rule("DRY-CONFIG-001", "code-quality", "high", "component",
        "When defining configuration values: feature flags, thresholds, environment URLs, retry counts, timeouts, fee rates.",
        "Each configuration value has exactly one source of truth (env var, config file, constants module, feature-flag service). Multiple definitions across files are violations; the single definition is the canonical and all callers reference it.",
        "```python\n# config.py: MAX_RETRIES = 3\n# worker.py: max_retries = 3\n# scheduler.py: 3 retries hardcoded inline\n```",
        "```python\n# config.py: MAX_RETRIES = int(os.environ.get('MAX_RETRIES', '3'))\n# all callers: from config import MAX_RETRIES\n```",
        "Code review.",
        "Config drift breaks the assumption that one place controls behavior. A single source of truth restores the invariant.",
        "3"),
    _rule("DRY-CONFIG-002", "code-quality", "medium", "component",
        "When introducing or modifying a feature flag.",
        "Feature flags are defined in a single registry (feature-flag module, env var, flag service). Inline `if env == 'prod'` or `if VERSION == 'v2'` checks scattered across modules are violations.",
        "```python\n# users.py: if get_env() == 'prod': new_path() else: old_path()\n# orders.py: if os.environ['STAGE'] == 'prod': ...\n```",
        "```python\n# flags.py: feature(name='new_user_flow', default=False)\n# all callers: if flags.is_enabled('new_user_flow'): new_path()\n```",
        "Code review.",
        "Scattered flag checks lose the ability to flip a flag in one place. Centralization is the structural defense for the next change.",
        "3"),
    _rule("DRY-TEMPLATE-001", "code-quality", "medium", "component",
        "When building UI views that repeat the same visual pattern in multiple pages.",
        "Repeated UI patterns (cards, modals, form rows, error displays) are extracted to shared components, not copied between views. A shared component is the source of truth for the pattern's behavior and styling.",
        "```jsx\n// Each page renders <div className='card'>...</div> with copied markup.\n```",
        "```jsx\n// components/Card.tsx exported; all pages import <Card>.\n```",
        "Code review.",
        "Copy-pasted UI fragments drift visually and behaviorally. A shared component anchors the pattern.",
        "3"),
    _rule("DRY-QUERY-001", "code-quality", "medium", "component",
        "When writing database queries from handler or service code.",
        "Common queries are wrapped in repository or DAO methods, not inlined at every call site. A given query (`active users by tenant`, `unpaid invoices older than N`) lives in one method.",
        "```python\n# handler1: User.query.filter(User.active==True, User.tenant_id==t).all()\n# handler2: same query, copy-pasted\n```",
        "```python\n# repository.py: def active_users(tenant_id): ...\n# both handlers call active_users(tenant_id)\n```",
        "Code review.",
        "Inlined queries duplicate filter logic and indexing assumptions. A repository method holds the canonical form.",
        "3"),
    _rule("DRY-TYPE-001", "code-quality", "medium", "component",
        "When declaring types or interfaces consumed by multiple modules.",
        "Shared types/interfaces are defined once and imported, not redeclared in each consumer. TypeScript `interface User` repeated in three files is a violation; one definition + three imports is the structure.",
        "```typescript\n// userService.ts: interface User { id: number; name: string; }\n// orderService.ts: interface User { id: number; name: string; }\n// inventoryService.ts: interface User { id: number; name: string; }\n```",
        "```typescript\n// types/user.ts: export interface User { id: number; name: string; }\n// all services: import { User } from '../types/user';\n```",
        "Code review.",
        "Drifting type definitions create silent mismatches at module boundaries. A single source aligns every consumer.",
        "3"),
]


RULES = CLEAN_RULES + DRY_RULES


async def main() -> None:
    db = connect()
    try:
        async with db._driver.session(database=db._database) as session:
            # Rename legacy ARCH-* rules to public-rulebook IDs.
            renames = [
                ("ARCH-FUNC-001", "CLEAN-FUNC-002"),
                ("ARCH-CONST-001", "CLEAN-MAGIC-001"),
                ("ARCH-ERR-001", "CLEAN-ERR-001"),
                ("ARCH-DRY-001", "DRY-DUP-001"),
                ("ARCH-SSOT-001", "DRY-CONFIG-001"),
            ]
            for old, new in renames:
                await session.run(
                    "MATCH (r:Rule {rule_id: $old}) DETACH DELETE r", old=old
                )
                print(f"DELETED {old:20s} (absorbed into {new})")

            # Upsert the 33 CLEAN-*, DRY-* rules.
            created = updated = 0
            for rule in RULES:
                existed = await upsert_rule(session, rule)
                if existed:
                    updated += 1
                    print(f"UPDATED {rule['rule_id']:30s} {rule['severity']}")
                else:
                    created += 1
                    print(f"CREATED {rule['rule_id']:30s} {rule['severity']}")

            print()
            print(f"Summary: {created} created, {updated} updated.")

            r = await session.run("MATCH (r:Rule) RETURN count(r) AS n")
            print(f"Total rules: {(await r.single())['n']}")
            r = await session.run("MATCH (r:Rule) WHERE r.mandatory = true RETURN count(r) AS n")
            print(f"Mandatory: {(await r.single())['n']}")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
