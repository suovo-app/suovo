#!/usr/bin/env python3
"""Validate the generated public Suovo-MCP reference against its schema bundle."""

from __future__ import annotations

import html
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

_SCRIPT = Path(__file__).resolve()
ROOT = _SCRIPT.parents[2] if _SCRIPT.parent.name == "scripts" else _SCRIPT.parents[1]
SITE = ROOT / "site"
CONTRACT = SITE / "product-mcp-contract-v1.json"
PUBLIC_FILES = [ROOT / "README.md", *sorted(SITE.glob("*.html")), SITE / "style.css", CONTRACT]


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.json_blocks: list[str] = []
        self.in_json = False
        self.parts: list[str] = []
        self.in_tool = ""
        self.tool_rows: dict[str, dict[str, str]] = {}
        self.fields: set[tuple[str, str, str]] = set()
        self.contract_meta: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag in {"a", "link", "script", "img"}:
            target = values.get("href") or values.get("src")
            if target:
                self.links.append(target)
        if tag == "article" and values.get("data-contract-tool"):
            name = values["data-contract-tool"] or ""
            self.in_tool = name
            self.tool_rows[name] = {
                key.removeprefix("data-"): value or ""
                for key, value in values.items()
                if key.startswith("data-")
            }
        if tag == "tr" and self.in_tool and values.get("data-contract-field"):
            self.fields.add((
                self.in_tool,
                values["data-contract-field"] or "",
                values.get("data-field-path") or "",
            ))
        if tag == "p" and "data-contract-version" in values:
            self.contract_meta = {
                key.removeprefix("data-"): value or ""
                for key, value in values.items()
                if key.startswith("data-")
            }
        if tag == "pre" and "language-json" in (values.get("class") or "").split():
            self.in_json = True
            self.parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "article" and self.in_tool:
            self.in_tool = ""
        if tag == "pre" and self.in_json:
            self.json_blocks.append("".join(self.parts))
            self.in_json = False

    def handle_data(self, data: str) -> None:
        if self.in_json:
            self.parts.append(data)


def schema_paths(schema: dict) -> set[str]:
    definitions = schema.get("$defs", {})
    paths: set[str] = set()

    def resolve(node: dict, stack: frozenset[str]) -> dict:
        ref = node.get("$ref")
        if not ref:
            return node
        name = ref.removeprefix("#/$defs/")
        if not ref.startswith("#/$defs/") or name not in definitions or name in stack:
            raise ValueError(f"unresolved or recursive reference {ref}")
        return resolve(definitions[name], stack | {name})

    def walk(node: dict, prefix: str = "") -> None:
        node = resolve(node, frozenset())
        for name, child in node.get("properties", {}).items():
            path = f"{prefix}.{name}" if prefix else name
            paths.add(path)
            walk(child, path)
        items = node.get("items")
        if isinstance(items, dict):
            walk(items, prefix + "[]")
        for key in ("oneOf", "anyOf", "allOf"):
            for branch in node.get(key, []):
                if isinstance(branch, dict):
                    walk(branch, prefix)

    walk(schema)
    return paths


def main() -> None:
    errors: list[str] = []
    parsed: dict[Path, PageParser] = {}
    for path in PUBLIC_FILES:
        if not path.is_file():
            errors.append(f"missing expected public file: {path.relative_to(ROOT)}")
            continue
        content = path.read_text(encoding="utf-8")
        for pattern, label in [
            (r"\bApp/(?:src-tauri|python-sidecar)(?:/|\b)", "internal source path"),
            (r"/Users/[^\s<'\"]+", "personal filesystem path"),
            (r"(?i)Bearer\s+(?!REPLACE_WITH_TOKEN_FROM_SUOVO_SETTINGS\b)[A-Za-z0-9._~-]{32,}", "bearer-looking secret"),
            (r"\b[0-9a-fA-F]{64}\b", "token-looking 64-character value"),
        ]:
            if re.search(pattern, content):
                errors.append(f"{path.relative_to(ROOT)} contains a {label}")
        if path.suffix == ".html":
            parser = PageParser()
            try:
                parser.feed(content)
                parser.close()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{path.relative_to(ROOT)}: HTML parse error: {exc}")
            parsed[path] = parser
            for snippet in parser.json_blocks:
                try:
                    json.loads(html.unescape(snippet))
                except json.JSONDecodeError as exc:
                    errors.append(f"{path.relative_to(ROOT)}: invalid JSON example: {exc}")

    for page, parser in parsed.items():
        for link in parser.links:
            split = urlsplit(link)
            if split.scheme or split.netloc or not split.path:
                continue
            target = (page.parent / unquote(split.path)).resolve()
            if not target.exists():
                errors.append(f"{page.relative_to(ROOT)} has broken local link: {link}")
            elif split.fragment and target.suffix == ".html":
                fragment = unquote(split.fragment)
                if not re.search(rf"\bid=[\"']{re.escape(fragment)}[\"']", target.read_text(encoding="utf-8")):
                    errors.append(f"{page.relative_to(ROOT)} has missing anchor: {link}")

    try:
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        expected_top = {"$schema", "contract_version", "schema_dialect", "result_format", "result_envelope", "delivery_capabilities", "tools"}
        if set(contract) != expected_top:
            errors.append("public Suovo-MCP contract has unknown or missing top-level fields")
        if contract.get("result_format") != "structuredContent":
            errors.append("contract result format is not structuredContent")
        if contract.get("delivery_capabilities") != []:
            errors.append("contract must declare no delivery capabilities")
        tools = contract.get("tools", {})
        if not isinstance(tools, dict) or len(tools) != 13:
            errors.append("contract must describe the 13 current tools")
        tool_page_path = SITE / "tools.html"
        page = tool_page_path.read_text(encoding="utf-8")
        parser = parsed[tool_page_path]
        if set(parser.tool_rows) != set(tools):
            errors.append("generated Tools page inventory differs from the schema bundle")
        meta = parser.contract_meta or {}
        if meta.get("contract-version") != contract.get("contract_version"):
            errors.append("Tools page contract version differs from schema bundle")
        if meta.get("result-format") != contract.get("result_format"):
            errors.append("Tools page result format differs from schema bundle")
        if meta.get("delivery-capabilities"):
            errors.append("Tools page must state that delivery capabilities are empty")
        expected_fields: set[tuple[str, str, str]] = set()
        for name, record in tools.items():
            row = parser.tool_rows.get(name)
            if row is None:
                continue
            expected_attrs = {
                "description": record["description"],
                "input-fields": set(record["input_fields"]),
                "required-input-fields": set(record["required_input_fields"]),
                "output-fields": set(record["output_fields"]),
                "required-output-fields": set(record["required_output_fields"]),
                "side-effects": record["side_effects"],
                "license-required": str(record["license_required"]).lower(),
            }
            for key in ("description", "side-effects", "license-required"):
                if row.get(key) != expected_attrs[key]:
                    errors.append(f"{name} reference differs from {key}")
            for key in ("input-fields", "required-input-fields", "output-fields", "required-output-fields"):
                if set(filter(None, row.get(key, "").split(","))) != expected_attrs[key]:
                    errors.append(f"{name} reference differs from {key}")
            expected_fields.update((name, "input", path) for path in schema_paths(record["input_schema"]))
            expected_fields.update((name, "output", path) for path in schema_paths(record["output_schema"]))
            if set(record["field_descriptions"]) != schema_paths(record["output_schema"]):
                errors.append(f"{name} field descriptions do not cover the output schema")
            if record["description"] not in html.unescape(page):
                errors.append(f"{name} description is not rendered")

        if parser.fields != expected_fields:
            errors.append(
                f"Tools page field paths differ from schemas: missing={sorted(expected_fields-parser.fields)}, "
                f"extra={sorted(parser.fields-expected_fields)}"
            )
        if "product-mcp-contract-v1.json" not in page:
            errors.append("Tools page must link to the downloadable contract bundle")
    except (OSError, json.JSONDecodeError, TypeError, KeyError, ValueError) as exc:
        errors.append(f"cannot validate public schema bundle against Tools page: {exc}")

    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        raise SystemExit(1)
    print(f"Validated {len(parsed)} HTML pages, all tool schema field paths, examples, local links, and public-data scans.")


if __name__ == "__main__":
    main()
