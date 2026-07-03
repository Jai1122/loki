"""Source inventory for a Spring Boot repository (DESIGN.md §4.2).

Parsing strategy is deliberately resilient. Target repos are Java 21, which uses
records, sealed types, pattern matching, and text blocks that older pure-Python
Java parsers can choke on. So:

1. We *try* ``javalang`` for a precise parse.
2. If it raises (unsupported syntax), we fall back to a masking/regex extractor
   built on :mod:`loki.javatext` that reliably recovers the facts LOKI actually
   needs — package, type kind, annotations, constructor parameters, injected
   fields, public method count, and a complexity score.

Either way parsing never throws to the caller; an unparseable file yields an
empty list and is simply skipped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from loki import javatext

try:  # javalang is optional; the regex fallback covers its absence or failure.
    import javalang
    from javalang import tree as _jtree

    _HAVE_JAVALANG = True
except Exception:  # pragma: no cover - exercised only where javalang is missing
    _HAVE_JAVALANG = False

_PACKAGE = re.compile(r"\bpackage\s+([\w.]+)\s*;")
_STEREOTYPES = {
    "Service", "Component", "Repository", "RestController", "Controller",
    "Configuration", "SpringBootApplication", "ControllerAdvice", "RestControllerAdvice",
}
_INJECT_ANNOTATIONS = {"Autowired", "Inject", "Resource"}
_ANNOTATION = re.compile(r"@([A-Za-z_$][\w$.]*)")


@dataclass
class MethodInfo:
    name: str
    parameter_types: list[str] = field(default_factory=list)
    return_type: str = "void"
    throws: list[str] = field(default_factory=list)
    is_public: bool = True


@dataclass
class FieldInfo:
    name: str
    type: str
    annotations: list[str] = field(default_factory=list)

    @property
    def is_injected(self) -> bool:
        return any(a in _INJECT_ANNOTATIONS for a in self.annotations)


@dataclass
class ClassInfo:
    fqcn: str
    name: str
    package: str
    kind: str  # class | interface | enum | record
    source_path: str
    annotations: list[str] = field(default_factory=list)
    methods: list[MethodInfo] = field(default_factory=list)
    fields: list[FieldInfo] = field(default_factory=list)
    constructor_param_types: list[str] = field(default_factory=list)
    is_abstract: bool = False
    complexity: int = 0

    @property
    def public_methods(self) -> list[MethodInfo]:
        return [m for m in self.methods if m.is_public]

    @property
    def stereotype(self) -> str | None:
        for a in self.annotations:
            simple = a.rsplit(".", 1)[-1]
            if simple in _STEREOTYPES:
                return simple
        return None

    @property
    def is_controller(self) -> bool:
        return self.stereotype in ("RestController", "Controller")


def parse_java_source(source: str, source_path: str) -> list[ClassInfo]:
    """Parse a ``.java`` file into per-top-level-type :class:`ClassInfo`."""
    package = _package_of(source)
    if _HAVE_JAVALANG:
        try:
            return _from_javalang(source, source_path, package)
        except Exception:
            pass  # fall through to the resilient extractor
    return _from_regex(source, source_path, package)


def _package_of(source: str) -> str:
    m = _PACKAGE.search(javatext.mask(source))
    if not m:
        return ""
    # package name lives in original source at the same offsets (it is code).
    return source[m.start(1) : m.end(1)]


# --- javalang path --------------------------------------------------------

def _from_javalang(source: str, source_path: str, package: str) -> list[ClassInfo]:
    tree = javalang.parse.parse(source)
    pkg = tree.package.name if tree.package else package
    bodies = {t.name: t.body for t in javatext.top_level_types(source)}
    infos: list[ClassInfo] = []
    for type_decl in tree.types:
        kind = _kind_of(type_decl)
        if kind is None:
            continue
        annotations = [a.name for a in (type_decl.annotations or [])]
        methods: list[MethodInfo] = []
        fields: list[FieldInfo] = []
        ctor_params: list[str] = []
        for member in getattr(type_decl, "body", []) or []:
            if isinstance(member, _jtree.MethodDeclaration):
                methods.append(_method_from_javalang(member))
            elif isinstance(member, _jtree.ConstructorDeclaration):
                if not ctor_params:  # first (typically only) constructor
                    ctor_params = [_type_name(p.type) for p in member.parameters]
            elif isinstance(member, _jtree.FieldDeclaration):
                fields.extend(_fields_from_javalang(member))
        infos.append(
            ClassInfo(
                fqcn=f"{pkg}.{type_decl.name}" if pkg else type_decl.name,
                name=type_decl.name,
                package=pkg,
                kind=kind,
                source_path=source_path,
                annotations=annotations,
                methods=methods,
                fields=fields,
                constructor_param_types=ctor_params,
                is_abstract="abstract" in (type_decl.modifiers or set()),
                complexity=javatext.control_flow_complexity(bodies.get(type_decl.name, "")),
            )
        )
    return infos


def _kind_of(type_decl: object) -> str | None:
    if isinstance(type_decl, _jtree.InterfaceDeclaration):
        return "interface"
    if isinstance(type_decl, _jtree.EnumDeclaration):
        return "enum"
    if isinstance(type_decl, _jtree.ClassDeclaration):
        return "class"
    return None


def _method_from_javalang(member: object) -> MethodInfo:
    return MethodInfo(
        name=member.name,
        parameter_types=[_type_name(p.type) for p in member.parameters],
        return_type=_type_name(member.return_type) if member.return_type else "void",
        throws=list(member.throws or []),
        is_public="public" in (member.modifiers or set())
        or not (member.modifiers or set()) & {"private", "protected"},
    )


def _fields_from_javalang(member: object) -> list[FieldInfo]:
    annotations = [a.name for a in (member.annotations or [])]
    type_name = _type_name(member.type)
    return [FieldInfo(name=d.name, type=type_name, annotations=annotations) for d in member.declarators]


def _type_name(type_node: object) -> str:
    if type_node is None:
        return "void"
    return getattr(type_node, "name", str(type_node))


# --- regex fallback path --------------------------------------------------

def _from_regex(source: str, source_path: str, package: str) -> list[ClassInfo]:
    infos: list[ClassInfo] = []
    for decl in javatext.top_level_types(source):
        annotations = [_ANNOTATION_simple(a) for a in _ANNOTATION.findall(decl.annotations)]
        ctor_params = _regex_constructor_params(decl.name, decl.body)
        fields = _regex_fields(decl.body)
        methods = _regex_methods(decl.name, decl.body)
        infos.append(
            ClassInfo(
                fqcn=f"{package}.{decl.name}" if package else decl.name,
                name=decl.name,
                package=package,
                kind=decl.kind,
                source_path=source_path,
                annotations=annotations,
                methods=methods,
                fields=fields,
                constructor_param_types=ctor_params,
                is_abstract="abstract" in decl.header.split(decl.name)[0],
                complexity=javatext.control_flow_complexity(decl.body),
            )
        )
    return infos


def _ANNOTATION_simple(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def _regex_constructor_params(class_name: str, body: str) -> list[str]:
    masked = javatext.mask(body)
    m = re.search(rf"\b{re.escape(class_name)}\s*\(", masked)
    if not m:
        return []
    inner, close = javatext.extract_argument(masked, m.end() - 1)
    if close == -1:
        return []
    return _param_types(inner)


def _param_types(inner: str) -> list[str]:
    types: list[str] = []
    for part in javatext.split_top_level_args(inner):
        part = re.sub(r"^(final\s+|@[\w$.]+\s+)+", "", part.strip())
        tokens = part.split()
        if len(tokens) >= 2:
            types.append(tokens[-2].split("<")[0])
    return types


def _regex_fields(body: str) -> list[FieldInfo]:
    fields: list[FieldInfo] = []
    masked = javatext.mask(body)
    # Field: optional annotations, modifiers, Type name [= ...];  at any position.
    pattern = re.compile(
        r"(?P<ann>(?:@[\w$.]+(?:\([^)]*\))?\s*)*)"
        r"(?:(?:public|private|protected|static|final|transient|volatile)\s+)*"
        r"(?P<type>[A-Za-z_$][\w$.]*(?:<[^;{}()]*>)?)\s+"
        r"(?P<name>[A-Za-z_$][\w$]*)\s*(?:=|;)"
    )
    for m in pattern.finditer(masked):
        # Skip anything that looks like a method (has '(' before the terminator).
        segment = masked[m.start() : m.end()]
        if "(" in segment.split(m.group("name"))[0]:
            continue
        ann_text = body[m.start("ann") : m.end("ann")]
        annotations = [_ANNOTATION_simple(a) for a in _ANNOTATION.findall(ann_text)]
        fields.append(FieldInfo(name=m.group("name"), type=m.group("type").split("<")[0], annotations=annotations))
    return fields


def _regex_methods(class_name: str, body: str) -> list[MethodInfo]:
    methods: list[MethodInfo] = []
    masked = javatext.mask(body)
    pattern = re.compile(
        r"(?P<mods>(?:(?:public|private|protected|static|final|abstract|default|synchronized)\s+)*)"
        r"(?P<ret>[A-Za-z_$][\w$.]*(?:<[^;{}]*>)?(?:\[\])?)\s+"
        r"(?P<name>[A-Za-z_$][\w$]*)\s*\("
    )
    for m in pattern.finditer(masked):
        name = m.group("name")
        if name == class_name or m.group("ret") in ("return", "new", "throw"):
            continue
        inner, close = javatext.extract_argument(masked, m.end() - 1)
        if close == -1:
            continue
        mods = m.group("mods")
        is_public = "public" in mods or not ("private" in mods or "protected" in mods)
        methods.append(
            MethodInfo(
                name=name,
                parameter_types=_param_types(inner),
                return_type=m.group("ret").split("<")[0],
                is_public=is_public,
            )
        )
    return methods


# --- repository walk ------------------------------------------------------

@dataclass
class Module:
    name: str
    root: Path
    main_src: Path
    test_src: Path


def discover_modules(repo_root: str | Path) -> list[Module]:
    """Find Gradle modules by locating ``src/main/java`` directories."""
    root = Path(repo_root)
    modules: list[Module] = []
    for main_src in sorted(root.rglob("src/main/java")):
        if not main_src.is_dir():
            continue
        module_root = main_src.parent.parent.parent  # java -> main -> src -> module
        rel = module_root.relative_to(root)
        name = str(rel) if str(rel) != "." else module_root.name
        modules.append(
            Module(
                name=name,
                root=module_root,
                main_src=main_src,
                test_src=module_root / "src" / "test" / "java",
            )
        )
    return modules
