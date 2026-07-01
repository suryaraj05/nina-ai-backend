"""Extract stable DOM anchors from crawled HTML."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any

_SIZE_LABEL = re.compile(
    r"^(?:size\s+)?(XXS|XS|S|M|L|XL|XXL|2XL|3XL|\d{2})$",
    re.IGNORECASE,
)


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.search_inputs: list[dict[str, str]] = []
        self.buttons: list[dict[str, str]] = []
        self.links: list[dict[str, str]] = []
        self.forms: list[dict[str, str]] = []
        self.size_options: list[str] = []
        self._stack: list[str] = []
        self._text_parts: list[str] = []
        self._capture_text = False
        self._in_size_select = False

    def handle_data(self, data: str) -> None:
        if self._capture_text and data.strip():
            self._text_parts.append(data.strip())
        if self._in_size_select:
            label = data.strip()
            if label and _SIZE_LABEL.match(label):
                self.size_options.append(label.upper())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._stack.append(tag)
        attr = {k: (v or "") for k, v in attrs}
        if tag == "button":
            self._text_parts = []
            self._capture_text = True
        if tag == "select":
            name = attr.get("name", "").lower()
            sid = attr.get("id", "").lower()
            if "size" in name or "size" in sid or "variant" in name:
                self._in_size_select = True
        if tag == "input":
            itype = attr.get("type", "text").lower()
            name = attr.get("name", "").lower()
            if "size" in name or "variant" in name:
                sel = _selector_for(tag, attr)
                if sel:
                    self.size_options.append(name)
            if itype in ("search", "text") or "search" in name:
                sel = _selector_for(tag, attr)
                if sel:
                    self.search_inputs.append({"selector": sel, "name": attr.get("name", "")})
        elif tag == "button":
            sel = _selector_for(tag, attr)
            if sel:
                self.buttons.append({
                    "selector": sel,
                    "label": attr.get("aria-label", ""),
                    "text": "",
                })
        elif tag == "a" and attr.get("href"):
            sel = _selector_for(tag, attr)
            if sel:
                self.links.append({"selector": sel, "href": attr.get("href", "")})
        elif tag == "form":
            sel = _selector_for(tag, attr)
            if sel:
                self.forms.append({"selector": sel, "action": attr.get("action", "")})

    def handle_endtag(self, tag: str) -> None:
        if tag == "button":
            text = " ".join(self._text_parts).strip()
            if text and _SIZE_LABEL.match(text):
                self.size_options.append(text.upper())
            if self.buttons:
                self.buttons[-1]["text"] = text
            self._text_parts = []
            self._capture_text = False
        if tag == "select":
            self._in_size_select = False
        if self._stack and self._stack[-1] == tag:
            self._stack.pop()


def _selector_for(tag: str, attr: dict[str, str]) -> str | None:
    if attr.get("data-testid"):
        return f'[data-testid="{attr["data-testid"]}"]'
    if attr.get("id"):
        return f"#{attr['id']}"
    if attr.get("name"):
        return f'{tag}[name="{attr["name"]}"]'
    if attr.get("aria-label"):
        return f'{tag}[aria-label="{attr["aria-label"]}"]'
    return None


def extract_dom_signals(html: str) -> dict[str, Any]:
    """Return search inputs, buttons, forms found in HTML."""
    parser = _AnchorParser()
    try:
        parser.feed(html or "")
    except Exception:
        pass
    return {
        "searchInputs": parser.search_inputs[:5],
        "buttons": parser.buttons[:10],
        "forms": parser.forms[:5],
        "links": parser.links[:20],
        "sizeOptions": _unique_size_labels(parser.size_options),
    }


def _unique_size_labels(raw: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        label = str(item).strip().upper()
        if not label or label in seen:
            continue
        if _SIZE_LABEL.match(label):
            seen.add(label)
            out.append(label)
    return out[:12]


def size_labels_from_signals(signals: dict[str, Any]) -> list[str]:
    """Collect apparel size labels from extracted DOM signals."""
    labels = list(signals.get("sizeOptions") or [])
    for btn in signals.get("buttons") or []:
        for key in ("text", "label"):
            text = str((btn or {}).get(key) or "").strip()
            if text and _SIZE_LABEL.match(text):
                labels.append(text.upper())
    return _unique_size_labels(labels)


def summarize_contract_signals(dom_by_type: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Persist crawl-derived UX signals on the agent contract for runtime skill synth."""
    pdp_types = ("product_detail", "pdp", "product")
    pdp_signals: dict[str, Any] = {}
    all_labels: list[str] = []
    for ptype in pdp_types:
        if ptype in dom_by_type:
            pdp_signals = dom_by_type[ptype]
            break
    if pdp_signals:
        all_labels = size_labels_from_signals(pdp_signals)
    if not all_labels:
        for signals in dom_by_type.values():
            found = size_labels_from_signals(signals)
            if found:
                all_labels = found
                break
    return {
        "productDetail": {
            "hasSizeOptions": bool(all_labels),
            "sizeLabels": all_labels,
        },
    }


def page_signals_from_crawl(pages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Aggregate DOM signals keyed by pageType."""
    by_type: dict[str, dict[str, Any]] = {}
    for page in pages:
        ptype = page.get("pageType", "generic")
        signals = extract_dom_signals(page.get("html", ""))
        if ptype not in by_type:
            by_type[ptype] = signals
        else:
            for key in ("searchInputs", "buttons", "forms"):
                existing = by_type[ptype].setdefault(key, [])
                for item in signals.get(key, []):
                    if item not in existing:
                        existing.append(item)
    return by_type
