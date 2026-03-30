#!/bin/bash
# Dependency scanner — extracts imports/use statements and verifies targets exist.
# Language-aware: PHP, JS/TS, Python, Go, Rust.
# Deterministic replacement for plan-guardian Pass 3.
#
# Usage:
#   bin/scan-deps.sh --project-root /path file1.php file2.ts ...
#   bin/scan-deps.sh file1.php   # auto-detect project root
#
# Output: { "files": { "src/Foo.php": { "imports": [...], "missing": [...] } }, "total_missing": N }

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"

PROJECT_ROOT=""
FILES=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-root) PROJECT_ROOT="$2"; shift 2 ;;
    *) FILES+=("$1"); shift ;;
  esac
done

if [ ${#FILES[@]} -eq 0 ]; then
  echo '{"error": "No files specified. Usage: scan-deps.sh [--project-root DIR] file1 [file2 ...]"}'
  exit 2
fi

if [ -z "$PROJECT_ROOT" ]; then
  PROJECT_ROOT=$(detect_project_root "${FILES[0]}")
fi

FILE_LIST=$(printf '%s\n' "${FILES[@]}")

export _PROJECT_ROOT="$PROJECT_ROOT"
export _FILE_LIST="$FILE_LIST"

python3 << 'PYTHON_SCRIPT'
import json, re, os, sys, glob as globmod

project_root = os.environ.get('_PROJECT_ROOT', '')
file_list_raw = os.environ.get('_FILE_LIST', '')

files = [f.strip() for f in file_list_raw.strip().split('\n') if f.strip()]
results = {}
total_missing = 0

def resolve(path):
    if os.path.isabs(path):
        return path
    if project_root:
        return os.path.join(project_root, path)
    return path

def find_php_class(namespace_class):
    """Convert PHP namespace to expected file path and check existence."""
    # Common autoload: Vendor\Module\Path\Class -> app/code/Vendor/Module/Path/Class.php
    parts = namespace_class.replace('\\', '/').split('/')
    basename = parts[-1] + '.php'

    # Check PSR-4 style paths
    if project_root:
        # Try direct namespace-to-path mapping
        candidate = os.path.join(project_root, namespace_class.replace('\\', '/') + '.php')
        if os.path.exists(candidate):
            return True

        # Try under app/code/
        candidate = os.path.join(project_root, 'app/code', namespace_class.replace('\\', '/') + '.php')
        if os.path.exists(candidate):
            return True

        # Try generated/code/
        candidate = os.path.join(project_root, 'generated/code', namespace_class.replace('\\', '/') + '.php')
        if os.path.exists(candidate):
            return True

        # Search for file by basename anywhere in project
        for root, dirs, filenames in os.walk(project_root):
            # Skip vendor, generated, var directories
            dirs[:] = [d for d in dirs if d not in ('vendor', '.git', 'var', 'pub', 'node_modules')]
            if basename in filenames:
                return True

    return False

def is_framework_namespace(ns, lang):
    """Return True if namespace is from a framework/vendor (not project-local)."""
    if lang == 'php':
        framework_prefixes = [
            'Magento\\', 'Psr\\', 'Symfony\\', 'Monolog\\', 'Composer\\',
            'PHPUnit\\', 'Laminas\\', 'GuzzleHttp\\', 'Psr7\\',
            'phpseclib\\', 'Firebase\\', 'Google\\', 'AWS\\',
        ]
        return any(ns.startswith(p) for p in framework_prefixes)
    if lang in ('javascript', 'typescript'):
        # node_modules imports don't start with . or /
        return not ns.startswith('.') and not ns.startswith('/')
    if lang == 'python':
        stdlib = {
            'os', 'sys', 'json', 're', 'math', 'datetime', 'collections',
            'itertools', 'functools', 'pathlib', 'typing', 'abc', 'io',
            'logging', 'unittest', 'argparse', 'subprocess', 'threading',
            'multiprocessing', 'socket', 'http', 'urllib', 'email',
            'html', 'xml', 'csv', 'sqlite3', 'hashlib', 'hmac',
            'secrets', 'copy', 'pprint', 'textwrap', 'struct',
            'enum', 'dataclasses', 'contextlib', 'inspect', 'traceback',
            'tempfile', 'shutil', 'glob', 'fnmatch', 'time', 'calendar',
            'random', 'statistics', 'decimal', 'fractions', 'operator',
            'string', 'codecs', 'unicodedata', 'difflib', 'heapq',
            'bisect', 'array', 'weakref', 'types', 'importlib',
            'pkgutil', 'pdb', 'profile', 'timeit', 'trace',
            'pickle', 'shelve', 'marshal', 'dbm', 'gzip', 'bz2',
            'zipfile', 'tarfile', 'configparser', 'signal', 'mmap',
            'ctypes', 'concurrent', 'asyncio', 'queue', 'sched',
        }
        top = ns.split('.')[0]
        return top in stdlib
    if lang == 'go':
        # Standard library has no dots in path
        return '.' not in ns and '/' not in ns
    return False

def scan_php(filepath):
    with open(filepath) as f:
        content = f.read()

    imports = []
    missing = []

    # Extract use statements
    for match in re.finditer(r'^\s*use\s+([\w\\]+)(?:\s+as\s+\w+)?;', content, re.MULTILINE):
        ns = match.group(1)
        imports.append(ns)
        if not is_framework_namespace(ns, 'php') and not find_php_class(ns):
            missing.append(ns)

    # Extract constructor type-hints
    ctor_match = re.search(r'function\s+__construct\s*\((.*?)\)', content, re.DOTALL)
    if ctor_match:
        params = ctor_match.group(1)
        for type_match in re.finditer(r'([\w\\]+)\s+\$\w+', params):
            type_name = type_match.group(1)
            if '\\' in type_name and type_name not in imports:
                imports.append(type_name)
                if not is_framework_namespace(type_name, 'php') and not find_php_class(type_name):
                    missing.append(type_name)

    return imports, missing

def scan_js_ts(filepath):
    with open(filepath) as f:
        content = f.read()

    imports = []
    missing = []

    # import ... from '...'
    for match in re.finditer(r'''(?:import|from)\s+['"]([^'"]+)['"]''', content):
        mod = match.group(1)
        imports.append(mod)

    # require('...')
    for match in re.finditer(r'''require\s*\(\s*['"]([^'"]+)['"]\s*\)''', content):
        mod = match.group(1)
        if mod not in imports:
            imports.append(mod)

    for mod in imports:
        if is_framework_namespace(mod, 'javascript'):
            continue
        # Resolve relative import
        base_dir = os.path.dirname(filepath)
        candidate = os.path.join(base_dir, mod)
        extensions = ['', '.ts', '.tsx', '.js', '.jsx', '/index.ts', '/index.js']
        found = any(os.path.exists(candidate + ext) for ext in extensions)
        if not found:
            missing.append(mod)

    return imports, missing

def scan_python(filepath):
    with open(filepath) as f:
        content = f.read()

    imports = []
    missing = []

    # import x / from x import y
    for match in re.finditer(r'^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))', content, re.MULTILINE):
        mod = match.group(1) or match.group(2)
        imports.append(mod)
        if is_framework_namespace(mod, 'python'):
            continue
        # Check if it's a local module
        top = mod.split('.')[0]
        base_dir = os.path.dirname(filepath)
        candidates = [
            os.path.join(base_dir, top + '.py'),
            os.path.join(base_dir, top, '__init__.py'),
        ]
        if project_root:
            candidates.extend([
                os.path.join(project_root, top + '.py'),
                os.path.join(project_root, top, '__init__.py'),
                os.path.join(project_root, 'src', top + '.py'),
                os.path.join(project_root, 'src', top, '__init__.py'),
            ])
        if not any(os.path.exists(c) for c in candidates):
            # Might be an installed package — don't flag unless clearly local
            if mod.startswith('.'):
                missing.append(mod)

    return imports, missing

def scan_go(filepath):
    with open(filepath) as f:
        content = f.read()

    imports = []
    missing = []

    # Single import
    for match in re.finditer(r'^\s*import\s+"([^"]+)"', content, re.MULTILINE):
        imports.append(match.group(1))

    # Block import
    for block in re.finditer(r'import\s*\((.*?)\)', content, re.DOTALL):
        for match in re.finditer(r'"([^"]+)"', block.group(1)):
            imports.append(match.group(1))

    # Check go.mod for module path
    module_path = ''
    if project_root:
        gomod = os.path.join(project_root, 'go.mod')
        if os.path.exists(gomod):
            with open(gomod) as f:
                for line in f:
                    m = re.match(r'module\s+(\S+)', line)
                    if m:
                        module_path = m.group(1)
                        break

    for imp in imports:
        if is_framework_namespace(imp, 'go'):
            continue
        if module_path and imp.startswith(module_path):
            # Project-local package
            pkg_path = imp[len(module_path):].lstrip('/')
            if project_root:
                pkg_dir = os.path.join(project_root, pkg_path)
                if not os.path.isdir(pkg_dir):
                    missing.append(imp)

    return imports, missing

def scan_rust(filepath):
    with open(filepath) as f:
        content = f.read()

    imports = []
    missing = []

    for match in re.finditer(r'^\s*(?:use|mod)\s+([\w:]+)', content, re.MULTILINE):
        mod = match.group(1)
        imports.append(mod)
        # Only flag crate-local modules
        if mod.startswith('crate::') or match.group(0).strip().startswith('mod '):
            mod_name = mod.replace('crate::', '').split('::')[0]
            if project_root:
                src_dir = os.path.join(project_root, 'src')
                candidates = [
                    os.path.join(src_dir, mod_name + '.rs'),
                    os.path.join(src_dir, mod_name, 'mod.rs'),
                ]
                if not any(os.path.exists(c) for c in candidates):
                    missing.append(mod)

    return imports, missing

def scan_graphql(filepath):
    with open(filepath) as f:
        content = f.read()

    imports = []
    missing = []

    for match in re.finditer(r'(?:class|cacheIdentity):\s*"([^"]+)"', content):
        classname = match.group(1)
        imports.append(classname)
        if not find_php_class(classname):
            missing.append(classname)

    return imports, missing


LANG_MAP = {
    '.php': ('php', scan_php),
    '.js': ('javascript', scan_js_ts),
    '.jsx': ('javascript', scan_js_ts),
    '.ts': ('typescript', scan_js_ts),
    '.tsx': ('typescript', scan_js_ts),
    '.py': ('python', scan_python),
    '.go': ('go', scan_go),
    '.rs': ('rust', scan_rust),
    '.graphqls': ('graphql', scan_graphql),
    '.graphql': ('graphql', scan_graphql),
}

for filepath in files:
    resolved = filepath
    if not os.path.isabs(filepath) and project_root:
        resolved = os.path.join(project_root, filepath)

    if not os.path.exists(resolved):
        results[filepath] = {
            'language': 'unknown',
            'imports': [],
            'missing': [],
            'error': 'File not found'
        }
        total_missing += 1
        continue

    ext = os.path.splitext(filepath)[1]
    if ext not in LANG_MAP:
        results[filepath] = {
            'language': 'unsupported',
            'imports': [],
            'missing': [],
        }
        continue

    lang, scanner = LANG_MAP[ext]
    try:
        imports, missing = scanner(resolved)
        results[filepath] = {
            'language': lang,
            'imports': imports,
            'missing': missing,
        }
        total_missing += len(missing)
    except Exception as e:
        results[filepath] = {
            'language': lang,
            'imports': [],
            'missing': [],
            'error': str(e)
        }

output = {
    'project_root': project_root,
    'files': results,
    'total_missing': total_missing,
    'clean': total_missing == 0
}

print(json.dumps(output, indent=2))
PYTHON_SCRIPT
