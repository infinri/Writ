# SQL Authoring

## Purpose

This document defines **SQL authoring standards** to ensure queries are readable, auditable, and maintainable.

---

<!-- RULE START: DB-SQL-001 -->
## Rule DB-SQL-001: Named Bind Parameters

**Domain**: Database / SQL
**Severity**: High
**Scope**: file

### Trigger
When writing a raw SQL query string that includes parameter placeholders.

### Statement
Use named bind keys (`:customerId`, `:status`) instead of positional `?` placeholders. Bind values in an associative array so names map to values in one place. Exception: long `IN (...)` lists may use positional binds.

### Violation (bad)
```php
$sql = "SELECT * FROM sales_order WHERE customer_id = ? AND status = ?";
$result = $connection->fetchAll($sql, [$customerId, $status]);
// Positional -- parameter order is fragile and unclear
```

### Pass (good)
```php
$sql = "SELECT * FROM sales_order WHERE customer_id = :customerId AND status = :status";
$result = $connection->fetchAll($sql, [':customerId' => $customerId, ':status' => $status]);
// Named -- self-documenting, order-independent
```

### Enforcement
Per-slice findings table (ENF-POST-006) must verify named binds in all raw SQL. Code review.

### Rationale
Named binds are self-documenting and reduce errors when modifying queries with many parameters. Positional binds become fragile as WHERE clause conditions are added or reordered.
<!-- RULE END: DB-SQL-001 -->

---

<!-- RULE START: DB-SQL-002 -->
## Rule DB-SQL-002: Minimal String Fragmentation

**Domain**: Database / SQL
**Severity**: Medium
**Scope**: file

### Trigger
When a raw SQL query is constructed using string concatenation (`.` operator in PHP) across more than 2 fragments without conditional logic requiring the split.

### Statement
Define SQL in a single heredoc or multi-line string. Split only when conditional WHERE clauses or dynamic JOINs require it.

### Violation (bad)
```php
$sql = "SELECT o.order_id, o.customer_id, c.name " .
       "FROM sales_order o " .
       "JOIN customer_entity c ON c.entity_id = o.customer_id " .
       "WHERE o.status = :status";
// Fragmented -- hard to copy for debugging, easy to miss trailing spaces
```

### Pass (good)
```php
$sql = <<<SQL
SELECT o.order_id, o.customer_id, c.name
FROM sales_order o
JOIN customer_entity c ON c.entity_id = o.customer_id
WHERE o.status = :status
SQL;
// Single heredoc -- copy-paste ready, no trailing-space bugs
```

### Enforcement
Per-slice findings table (ENF-POST-006). Code review.

### Rationale
Fragmented SQL is harder to read, copy for debugging, and audit for security issues. Missing trailing spaces in concatenated fragments is a common source of syntax errors.
<!-- RULE END: DB-SQL-002 -->

---

<!-- RULE START: DB-SQL-003 -->
## Rule DB-SQL-003: Readable SQL Formatting

**Domain**: Database / SQL
**Severity**: Medium
**Scope**: file

### Trigger
When writing any SQL query longer than one line.

### Statement
Format SQL vertically. Each JOIN on its own line with ON condition. Column lists comma-separated per line. WHERE conditions on separate lines with AND/OR alignment.

### Violation (bad)
```php
$sql = "SELECT o.order_id, o.customer_id, c.name FROM sales_order o JOIN customer_entity c ON c.entity_id = o.customer_id LEFT JOIN customer_address_entity a ON a.parent_id = c.entity_id WHERE o.status = :status AND o.created_at > :startDate ORDER BY o.created_at DESC";
```

### Pass (good)
```php
$sql = <<<SQL
SELECT
    o.order_id,
    o.customer_id,
    c.name
FROM sales_order o
JOIN customer_entity c ON c.entity_id = o.customer_id
LEFT JOIN customer_address_entity a ON a.parent_id = c.entity_id
WHERE o.status = :status
    AND o.created_at > :startDate
ORDER BY o.created_at DESC
SQL;
```

### Enforcement
Per-slice findings table (ENF-POST-006). Code review.

### Rationale
Readable SQL is easier to review, debug, and maintain. Vertical formatting makes complex queries scannable and diff-friendly.
<!-- RULE END: DB-SQL-003 -->
