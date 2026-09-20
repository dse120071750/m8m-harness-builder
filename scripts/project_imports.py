"""Scoped imports from an explicit frozen codebase module allowlist.

This module never adds a directory to sys.path. The runtime supplies the exact
codebase, ordered author declarations and already verified implementation files.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import importlib.machinery
import importlib.util
from pathlib import Path, PurePosixPath
import sys
import sysconfig
import threading


class ProjectImportError(ValueError):
    pass


def validate_roots(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 16:
        raise ProjectImportError('codebase_import_roots must contain one to sixteen ordered roots')
    if any(not isinstance(value, str) for value in values) or len(set(values)) != len(values):
        raise ProjectImportError('codebase_import_roots must contain distinct strings')
    for value in values:
        path = PurePosixPath(value)
        if (not value or '\\' in value or ':' in value or path.is_absolute()
                or '..' in path.parts or (value != '.' and ('.' in value.split('/') or str(path) != value))):
            raise ProjectImportError(f'unsafe codebase import root: {value!r}')
    return tuple(values)


def _safe_path(project, path):
    if not path.is_relative_to(project):
        raise ProjectImportError(f'import path escapes captured codebase: {path}')
    cursor = project
    for part in path.relative_to(project).parts:
        cursor = cursor / part
        if cursor.is_symlink() or bool(getattr(cursor, 'is_junction', lambda: False)()):
            raise ProjectImportError(f'import path uses an unsafe link: {cursor}')
    if path.resolve() != path:
        raise ProjectImportError(f'import path differs from captured physical path: {path}')


def module_map(project, values, files):
    """Return exact names/namespace parents; do not enumerate import directories."""
    project = Path(project).absolute()
    if project.is_symlink() or bool(getattr(project, 'is_junction', lambda: False)()):
        raise ProjectImportError('captured codebase cannot be an unsafe link')
    modules, roots = {}, validate_roots(values)
    frozen = {Path(path).absolute() for path in files if Path(path).suffix == '.py'}
    for relative in roots:
        root = project if relative == '.' else project.joinpath(*PurePosixPath(relative).parts)
        _safe_path(project, root)
        if not root.is_dir():
            raise ProjectImportError(f'declared import root is missing: {relative}')
        covered = False
        for path in sorted(frozen):
            if not path.is_relative_to(root):
                continue
            pieces = list(path.relative_to(root).with_suffix('').parts)
            package = pieces[-1:] == ['__init__']
            if package:
                pieces.pop()
            if not pieces or not all(part.isidentifier() for part in pieces):
                continue
            _safe_path(project, path)
            if not path.is_file():
                raise ProjectImportError(f'captured import file is missing: {path}')
            covered = True
            for count in range(1, len(pieces) + 1):
                name = '.'.join(pieces[:count])
                exact = count == len(pieces)
                location = path if exact else root.joinpath(*pieces[:count])
                record = (location, package if exact else True, exact)
                previous = modules.get(name)
                if previous is not None:
                    if previous[0] != location:
                        # A captured package initializer and its namespace
                        # parent designate the same physical package directory.
                        old_dir = previous[0].parent if previous[2] and previous[1] else previous[0]
                        new_dir = location.parent if exact and package else location
                        if old_dir != new_dir or (previous[2] and exact):
                            raise ProjectImportError(f'ambiguous captured module: {name}')
                    if previous[2]:
                        continue
                modules[name] = record
        if not covered:
            raise ProjectImportError(f'import root has no captured Python modules: {relative}')
    return project, roots, modules


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _BytesLoader:
    def __init__(self, path, digest):
        self.path, self.digest = path, digest

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        content = self.path.read_bytes()
        if hashlib.sha256(content).hexdigest() != self.digest:
            raise ProjectImportError(f'captured module bytes changed: {self.path}')
        exec(compile(content, str(self.path), 'exec'), module.__dict__)


class _Finder:
    def __init__(self, modules, digests):
        self.modules, self.digests = modules, digests
        self.tops = {name.split('.')[0] for name in modules}

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] not in self.tops:
            return None
        record = self.modules.get(fullname)
        if record is None:
            raise ModuleNotFoundError(f'project module is not captured: {fullname}', name=fullname)
        location, package, exact = record
        if not exact:
            spec = importlib.machinery.ModuleSpec(fullname, None, is_package=True)
            spec.submodule_search_locations = [str(location)]
            return spec
        return importlib.util.spec_from_file_location(fullname,
            location, loader=_BytesLoader(location, self.digests[location]),
            submodule_search_locations=[str(location.parent)] if package else None)


_LOCK = threading.RLock()
_ACTIVE = None
_CACHES = {}


class ImportContext:
    def __init__(self, project, roots, modules, implementation_fingerprint, all_digests):
        self.project, self.roots, self.modules = project, roots, modules
        self.digests = {row[0]: _hash(row[0]) for row in modules.values() if row[2]}
        self.all_digests = all_digests
        self.key = (str(project), roots, implementation_fingerprint)
        self.finder = _Finder(modules, self.digests)
        self.tops = self.finder.tops
        self.cache = _CACHES.setdefault(self.key, {})

    def _owned(self, name):
        return name.split('.')[0] in self.tops

    def _check_module(self, name, module):
        record = self.modules.get(name)
        if module is None or record is None:
            raise ProjectImportError(f'foreign cached project module: {name}')
        location, package, exact = record
        origin = getattr(module, '__file__', None)
        if exact and (not origin or Path(origin).absolute() != location):
            raise ProjectImportError(f'foreign cached project module: {name}')
        if not exact and origin:
            raise ProjectImportError(f'foreign cached namespace module: {name}')
        if package:
            expected = location.parent if exact else location
            actual = tuple(Path(value).absolute() for value in getattr(module, '__path__', ()))
            if actual != (expected,):
                raise ProjectImportError(f'foreign cached project package path: {name}')

    def _check_priority(self):
        stdlib = set(getattr(sys, 'stdlib_module_names', ())) | set(sys.builtin_module_names)
        trusted = {Path(sysconfig.get_path('stdlib')).absolute(), Path(sys.base_prefix).absolute()}
        # Runtime vendor roots are already verified by the immutable launcher;
        # installed site-packages serve the same role during Builder validation.
        trusted.update(Path(value).absolute() for value in sys.path if value and ('site-packages' in Path(value).parts or 'vendor' in Path(value).parts))
        for name in self.tops:
            if name in stdlib:
                raise ProjectImportError(f'project module would shadow stdlib: {name}')
            spec = importlib.machinery.PathFinder.find_spec(name)
            if spec:
                locations = list(spec.submodule_search_locations or ())
                if spec.origin and spec.origin not in {'built-in', 'frozen'}:
                    locations.append(spec.origin)
                for location in locations:
                    origin = Path(location).absolute()
                    if any(origin.is_relative_to(root) for root in trusted):
                        raise ProjectImportError(f'project module would shadow a trusted dependency: {name}')

    @contextmanager
    def activate(self):
        global _ACTIVE
        with _LOCK:
            if _ACTIVE is not None:
                if _ACTIVE.key != self.key:
                    raise ProjectImportError('cannot nest different captured codebase import contexts')
                yield
                return
            self._check_priority()
            for path, digest in self.all_digests.items():
                _safe_path(self.project, path)
                if _hash(path) != digest:
                    raise ProjectImportError(f'captured implementation bytes changed: {path}')
            for name, module in list(sys.modules.items()):
                if self._owned(name):
                    self._check_module(name, module)
                    # Origin alone is insufficient: an unrelated loader may
                    # have executed different bytes under a forged filename.
                    if self.cache.get(name) is not module:
                        raise ProjectImportError(f'foreign cached project module: {name}')
            for name, module in self.cache.items():
                self._check_module(name, module)
            sys.modules.update(self.cache)
            position = next((i for i, finder in enumerate(sys.meta_path) if finder is importlib.machinery.PathFinder), len(sys.meta_path))
            sys.meta_path.insert(position, self.finder)
            _ACTIVE = self
            try:
                yield
            finally:
                selected = {name: module for name, module in list(sys.modules.items()) if self._owned(name)}
                for name in selected:
                    sys.modules.pop(name, None)
                if self.finder in sys.meta_path:
                    sys.meta_path.remove(self.finder)
                _ACTIVE = None
                for name, module in selected.items():
                    self._check_module(name, module)
                self.cache.update(selected)


def import_context(project, roots, files):
    files = tuple(sorted({Path(path).absolute() for path in files}))
    fingerprint = hashlib.sha256()
    all_digests = {}
    for path in files:
        content = path.read_bytes()
        all_digests[path] = hashlib.sha256(content).hexdigest()
        fingerprint.update(str(path).encode('utf-8') + b'\0' + content + b'\0')
    return ImportContext(*module_map(project, roots, files), fingerprint.hexdigest(), all_digests)
