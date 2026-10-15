# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Bounded DOM observations for each frame, including open shadow roots."""

VISIBILITY_JS = r"""
    const roots = [document];
    for (let i = 0; i < roots.length; i++) {
        for (const el of roots[i].querySelectorAll('*')) {
            if (el.shadowRoot) roots.push(el.shadowRoot);
        }
    }
    const all = selector => roots.flatMap(root => Array.from(root.querySelectorAll(selector)));
    const width = window.innerWidth || document.documentElement.clientWidth;
    const height = window.innerHeight || document.documentElement.clientHeight;
    const parent = el => el.assignedSlot || el.parentElement || el.getRootNode().host;
    const modalAncestor = el => {
        for (let node = el; node; node = parent(node)) {
            if (node.matches('dialog:modal')) return node;
        }
        return null;
    };
    let focused = document.activeElement;
    while (focused?.shadowRoot?.activeElement) focused = focused.shadowRoot.activeElement;
    let modal = modalAncestor(focused);
    const modals = all('dialog:modal');
    if (!modal && modals.length) {
        // After blur(), the top-layer backdrop still identifies the active dialog.
        let hit = document.elementFromPoint(0, 0);
        while (hit?.shadowRoot) {
            const child = hit.shadowRoot.elementFromPoint(0, 0);
            if (!child || child === hit) break;
            hit = child;
        }
        modal = modalAncestor(hit) || modals[modals.length - 1];
    }
    const visible = (el, inViewport = true) => {
        const rect = el.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) return false;
        if (inViewport && (rect.bottom <= 0 || rect.right <= 0 ||
            rect.top >= height || rect.left >= width)) return false;
        if (modal && modalAncestor(el) !== modal) return false;
        const clips = value => /^(auto|scroll|hidden|clip|overlay)$/.test(value);
        let insideModal = false;
        for (let node = el; node; node = parent(node)) {
            if (!insideModal && node.inert) return false;
            // showModal() escapes inherited inertness, including across shadow roots.
            if (node.matches('dialog:modal')) insideModal = true;
            const style = getComputedStyle(node);
            if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0')
                return false;
            if (node === el) continue;
            const bounds = node.getBoundingClientRect();
            if (clips(style.overflowY) && (rect.bottom <= bounds.top || rect.top >= bounds.bottom))
                return false;
            if (clips(style.overflowX) && (rect.right <= bounds.left || rect.left >= bounds.right))
                return false;
        }
        return true;
    };
"""

FRAME_VISIBLE_JS = "el => {" + VISIBILITY_JS + "return visible(el, false); }"

SNAPSHOT_JS = r"""
({start, limit, offset = 0}) => {
""" + VISIBILITY_JS + r"""
    all('[data-mclaw-ref]').forEach(el => el.removeAttribute('data-mclaw-ref'));
    const compact = text => (text || '').replace(/\s+/g, ' ').trim();
    const label = el => {
        const labelled = (el.getAttribute('aria-labelledby') || '').split(/\s+/)
            .map(id => el.getRootNode().getElementById?.(id)?.textContent || '').join(' ');
        const associated = Array.from(el.labels || []).map(node => node.textContent).join(' ');
        const accessible = el.getAttribute('aria-label') || compact(labelled) || compact(associated);
        if (el.matches('input, textarea'))
            return compact(accessible || el.placeholder || (el.type === 'password' ? '' : el.value));
        return compact(accessible || compact(el.textContent) || el.getAttribute('title'));
    };
    const selectors = 'a,button,input:not([type="hidden"]),textarea,select,label,summary,' +
        '[role="button"],[role="link"],[role="checkbox"],[role="radio"],[role="switch"],' +
        '[role="textbox"],[role="searchbox"],[role="combobox"],[role="option"],' +
        '[role="tab"],[role="menuitem"],[contenteditable=""],[contenteditable="true" i],' +
        '[contenteditable="plaintext-only" i],[tabindex]';
    const candidates = all(selectors).filter(el => visible(el));
    const selected = candidates.slice(offset, offset + limit);
    const elements = selected.map((el, index) => {
        const ref = 'e' + (start + index);
        el.setAttribute('data-mclaw-ref', ref);
        const tag = el.tagName.toLowerCase();
        return {
            ref, tag, type: el.type || '', text: label(el).slice(0, 120),
            role: el.getAttribute('role') || (el.isContentEditable ? 'textbox' : ''), href: el.href || '',
            disabled: el.matches(':disabled') || el.getAttribute('aria-disabled') === 'true',
            multiple: Boolean(el.multiple),
            checked: el.matches('input[type="checkbox"],input[type="radio"]') ? el.checked :
                el.getAttribute('aria-checked'),
            value: el.matches('textarea,input:not([type="password"]):not([type="file"])') ?
                String(el.value || '').slice(0, 120) : undefined,
            optionCount: tag === 'select' ? el.options.length : undefined,
            options: tag === 'select' ? Array.from(el.options).slice(0, 30).map(option => ({
                value: option.value.length <= 160 ? option.value : null,
                label: compact(option.label).slice(0, 60), selected: option.selected
            })) : undefined
        };
    });
    const headings = all('h1,h2,h3,h4').filter(el => visible(el)).slice(0, 8)
        .map(el => ({level: Number(el.tagName[1]), text: compact(el.textContent).slice(0, 100)}));
    // Text nodes also cover SPA div/span markup. Checking each node excludes
    // hidden descendants that an ancestor's textContent would otherwise leak.
    const blocks = new Map();
    for (const root of roots) {
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
        for (let node; (node = walker.nextNode());) {
            const el = node.parentElement;
            const text = compact(node.textContent);
            if (!text || !el || !visible(el)) continue;
            let excluded = false;
            for (let ancestor = el; ancestor; ancestor = parent(ancestor)) {
                if (ancestor.matches(selectors + ',script,style,noscript,h1,h2,h3,h4')) {
                    excluded = true;
                    break;
                }
            }
            if (excluded) continue;
            const range = document.createRange();
            range.selectNodeContents(node);
            const rect = range.getBoundingClientRect();
            if (rect.bottom <= 0 || rect.top >= height || rect.right <= 0 || rect.left >= width) continue;
            const block = el.closest('p,div,li,td,th,article,section') || el;
            blocks.set(block, ((blocks.get(block) || '') + ' ' + text).slice(0, 220));
        }
    }
    const textBlocks = Array.from(new Set(Array.from(blocks.values(), compact))).slice(0, 12);
    const top = Math.max(0, Math.round(window.scrollY || 0));
    const pageHeight = Math.max(height, document.documentElement.scrollHeight,
        document.body ? document.body.scrollHeight : 0);
    return {elements, nodes: selected, headings, textBlocks, total: candidates.length,
        truncated: candidates.length > offset + limit,
        viewport: {top, bottom: Math.min(pageHeight, top + height), height, pageHeight,
            hasMoreAbove: top > 1, hasMoreBelow: top + height < pageHeight - 1}};
}
"""


def element_line(element: dict) -> str:
    """Render an actionable reference with its accessible label and state."""
    tag = element.get("tag", "")
    role = element.get("role") or {"a": "link", "summary": "button"}.get(tag, tag)
    if tag in {"input", "textarea"}:
        role = element.get("type") or tag
    label = str(element.get("text") or "").replace("\n", " ")[:100]
    line = f"[{element['ref']}] {role} '{label}'"
    if element.get("disabled"):
        line += " [disabled]"
    if element.get("multiple"):
        line += " [multiple]"
    checked = element.get("checked")
    if checked is not None:
        line += f" [checked={str(checked).lower()}]"
    if element.get("value"):
        line += f" value={element['value']!r}"
    if element.get("href"):
        line += f" → {element['href'][:160]}"
    if element.get("options"):
        options = []
        budget = 900
        for item in element["options"]:
            if item["value"] is None:
                continue
            option = f"{item['value']!r}={item['label']!r}" + ("*" if item['selected'] else "")
            if len(option) > budget:
                break
            options.append(option)
            budget -= len(option) + 2
        line += " options: " + ", ".join(options)
        if len(options) < element.get("optionCount", len(element["options"])):
            line += " [some options omitted; use browser_press to explore]"
    return line
