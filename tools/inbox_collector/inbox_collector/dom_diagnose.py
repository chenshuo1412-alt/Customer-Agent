"""Read-only structure of the page this process already owns.

The export does not navigate, click, read storage, or invent selectors.
data-v-* and tabindex are recorded only as unstable names, never as candidates.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

_SAFE_CLASS = re.compile(r"^[A-Za-z][A-Za-z_-]{0,48}$")
_UNSTABLE_NAMES = frozenset({"tabindex"})
# Fixture-only names. Their absence on a merchant page is not a collection verdict.
_FIXTURE_NAMES = frozenset({"data-message-id", "data-direction", "data-message-time"})
# Class tokens seen on the live page. They are landmarks for diagnosis, not selectors.
_OBSERVED_SHELL_CLASSES = frozenset({"chat-list-box", "chatWindowHeader"})
_TIME_ATTRIBUTE_NAMES = frozenset({"msgtimeformate", "threeminutesthreshold"})
_TEXT_CARRIER_NAMES = frozenset({"title", "aria-label", "href", "data-clipboard-text"})

# Values are classified inside the page. Only counts and safe class tokens leave it.
# Light-DOM landmark search is not capped at the old depth-8 export. Frames are
# entered only when the document is already readable and the origin/path is in scope.
_MESSAGE_LANDMARKS = frozenset({"content-box", "reply-box"})
_LIST_LANDMARKS = frozenset({"chat-list-box", "chatWindowHeader", "chat-item"})
_HISTORY_LANDMARKS = frozenset({"loadMoreMsg"})
_FRAME_CATEGORIES = frozenset(
    {
        "same_origin_chat",
        "local_test",
        "blank_nested",
        "unreadable",
        "login_or_credential",
        "payment",
        "off_scope",
    }
)
_FRAME_REASONS = frozenset(
    {
        "entered",
        "access_denied",
        "no_document",
        "password_field",
        "url_path",
        "not_chat_merchant_frame",
        "frame_nesting_limit",
        "top_page",
    }
)
_TRUNCATION_REASONS = frozenset(
    {
        "visit_limit",
        "depth_limit",
        "sample_limit",
        "sample_depth",
        "unreadable_frame",
        "shadow_visit_limit",
        "frame_nesting_limit",
    }
)
_EXPORT_JS = """
() => {
  const MESSAGE = new Set(["content-box", "reply-box"]);
  const LIST = new Set(["chat-list-box", "chatWindowHeader", "chat-item"]);
  const HISTORY = new Set(["loadMoreMsg"]);
  const VISIT_LIMIT = 12000;
  const DEPTH_LIMIT = 64;
  const FRAME_LIMIT = 3;
  const SAMPLE_DEPTH = 16;
  const nodes = [];
  const groups = [];
  const frames = [];
  const reasons = new Set();
  const seenDocs = new Set();
  const seenShadows = new Set();
  const processedFrames = new Set();
  const seenRows = new Set();
  const landmarkEls = [];
  let visits = 0;
  let maxDepth = 0;
  let omittedByDepth = 0;
  let openShadow = 0;
  let sampleOmitted = 0;
  const shell = { generic_shell: 0, other_shape: 0, long_numeric: 0, long: 0 };
  const sampleCounts = { message_container: 0, conversation_list: 0, unspecified: 0 };
  const sampleCaps = { message_container: 160, conversation_list: 40, unspecified: 40 };

  const safeClass = (token) => /^[A-Za-z][A-Za-z_-]{0,48}$/.test(token);
  const classesOf = (el) => {
    if (!el || !el.getAttribute) return [];
    const out = [];
    for (const token of String(el.getAttribute("class") || "").split(/\\s+/)) {
      if (token && safeClass(token)) out.push(token);
    }
    return out;
  };
  const isHidden = (el) => {
    let cur = el;
    let guard = 0;
    while (cur && cur.nodeType === 1 && guard < 40) {
      if (cur.hidden || (cur.getAttribute && cur.getAttribute("aria-hidden") === "true")) return true;
      try {
        const style = cur.ownerDocument.defaultView.getComputedStyle(cur);
        if (style.display === "none" || style.visibility === "hidden") return true;
      } catch (e) {
        return false;
      }
      const root = cur.getRootNode ? cur.getRootNode() : null;
      cur = cur.parentElement || (root && root.host) || null;
      guard += 1;
    }
    return false;
  };
  const idShape = (value) => {
    if (!value) return "empty";
    if (/^(app|root|header|footer|main|content|app-root)$/i.test(value)) return "generic_shell";
    if (value.length > 64) return "long";
    if (/^\\d{6,}$/.test(value)) return "long_numeric";
    return "other_shape";
  };
  const timeKind = (value) => {
    const text = String(value || "").trim();
    if (!text) return "empty";
    const template = /yyyy|YYYY|MM|dd|DD|HH|mm|ss/.test(text) && !/^\\d{4}-\\d{2}-\\d{2}/.test(text);
    if (template) return "format_description";
    if (/^\\d{4}-\\d{2}-\\d{2}(?:[T\\s]\\d{2}:\\d{2}(?::\\d{2})?)?/.test(text)) return "parseable_time";
    if (/^\\d{1,2}:\\d{2}(?::\\d{2})?$/.test(text)) return "clock_without_date";
    return "other_redacted";
  };
  const labelKind = (value) => {
    const text = String(value || "").trim();
    if (!text) return "empty";
    if (text === "买家") return "buyer";
    if (text === "商家") return "merchant";
    if (text === "已回复") return "replied";
    if (text === "未回复") return "unreplied";
    return "other_redacted";
  };
  const bump = (bag, key) => { bag[key] = (bag[key] || 0) + 1; };
  const pageKind = () => {
    const host = String(location.hostname || "").toLowerCase();
    const path = String(location.pathname || "").toLowerCase();
    const protocol = String(location.protocol || "");
    if (/login|passport|password|signin/.test(path)) return "login_or_credential";
    if (/pay|cashier|checkout|wallet/.test(path)) return "payment";
    if (host === "mms.pinduoduo.com" && (path === "/chat-merchant" || path.startsWith("/chat-merchant/"))) return "chat_merchant";
    if ((host === "127.0.0.1" || host === "localhost") && (protocol === "http:" || protocol === "https:")) return "local_test";
    if (protocol === "file:") return "local_test";
    return "off_scope";
  };
  const topKind = pageKind();
  const chainDepth = (el) => {
    let depth = 0;
    let cur = el;
    while (cur && depth < 80) {
      const root = cur.getRootNode ? cur.getRootNode() : null;
      const parent = cur.parentElement || (root && root.host) || null;
      if (!parent) break;
      cur = parent;
      depth += 1;
    }
    if (depth > maxDepth) maxDepth = depth;
    if (depth >= 80) reasons.add("depth_limit");
    return depth;
  };
  const regionOf = (el) => {
    let cur = el;
    let guard = 0;
    let sawList = false;
    while (cur && guard < 80) {
      const names = classesOf(cur);
      if (names.some((name) => MESSAGE.has(name))) return "message_container";
      if (names.some((name) => LIST.has(name))) sawList = true;
      const root = cur.getRootNode ? cur.getRootNode() : null;
      cur = cur.parentElement || (root && root.host) || null;
      guard += 1;
    }
    return sawList ? "conversation_list" : "unspecified";
  };
  const remember = (el, frameCategory, inShadow, inFrame) => {
    const names = classesOf(el);
    const wanted = names.filter((name) => MESSAGE.has(name) || LIST.has(name) || HISTORY.has(name));
    if (!wanted.length) return;
    landmarkEls.push({
      el,
      names: wanted,
      depth: chainDepth(el),
      hidden: isHidden(el),
      frame_category: frameCategory,
      in_shadow: inShadow === true,
      in_frame: inFrame === true,
      region: names.some((name) => MESSAGE.has(name)) ? "message_container" : (names.some((name) => LIST.has(name)) ? "conversation_list" : "history_control")
    });
  };
  const markLandmarks = (root, frameCategory, inShadow, inFrame) => {
    for (const name of ["content-box", "reply-box", "chat-item", "chat-list-box", "chatWindowHeader", "loadMoreMsg"]) {
      let found = [];
      try { found = root.querySelectorAll("." + name); } catch (e) { found = []; }
      for (const el of found) remember(el, frameCategory, inShadow, inFrame);
    }
  };
  const recordFrame = (category, reason, entered, nesting) => {
    frames.push({ category, reason, entered: entered === true, nesting });
  };
  const childElements = (el) => Array.from(el.children || []);
  const snapshotNode = (el, region, frameCategory, inShadow, depth) => {
    const attributes = [];
    let conversationSelected = null;
    for (const attr of Array.from(el.attributes || [])) {
      attributes.push(String(attr.name));
      if (attr.name === "data-conversation-selected" && (attr.value === "true" || attr.value === "false")) {
        conversationSelected = attr.value === "true";
      }
    }
    nodes.push({
      tag: String(el.tagName || "").toLowerCase(),
      attributes,
      class_structure: classesOf(el),
      path: classesOf(el).slice(0, 3),
      conversation_selected: conversationSelected,
      depth,
      child_element_count: el.children ? el.children.length : 0,
      region,
      frame_category: frameCategory,
      in_shadow: inShadow === true,
      hidden: isHidden(el)
    });
  };
  const exportTree = (el, region, frameCategory, inShadow, relative) => {
    if (!el || el.nodeType !== 1 || exported.has(el)) return;
    if (relative > SAMPLE_DEPTH) { reasons.add("sample_depth"); return; }
    const bucket = sampleCaps[region] ? region : "unspecified";
    if (sampleCounts[bucket] >= sampleCaps[bucket]) { sampleOmitted += 1; reasons.add("sample_limit"); return; }
    exported.add(el);
    sampleCounts[bucket] += 1;
    snapshotNode(el, bucket, frameCategory, inShadow, chainDepth(el));
    for (const child of childElements(el)) exportTree(child, bucket, frameCategory, inShadow, relative + 1);
    if (el.shadowRoot) {
      for (const child of childElements(el.shadowRoot)) exportTree(child, bucket, frameCategory, true, relative + 1);
    }
  };
  const visit = (parent, depth, frameCategory, inShadow) => {
    if (!parent || visits >= VISIT_LIMIT) { if (visits >= VISIT_LIMIT) reasons.add("visit_limit"); return; }
    if (depth > DEPTH_LIMIT) {
      reasons.add("depth_limit");
      omittedByDepth += childElements(parent).length;
      return;
    }
    const buckets = new Map();
    for (const child of childElements(parent)) {
      visits += 1;
      if (visits > VISIT_LIMIT) { reasons.add("visit_limit"); return; }
      const selected = child.getAttribute && child.getAttribute("data-conversation-selected");
      if (selected === "true" || selected === "false") snapshotNode(child, "unspecified", frameCategory, inShadow, depth + 1);
      if (child.shadowRoot && !seenShadows.has(child.shadowRoot)) {
        seenShadows.add(child.shadowRoot);
        openShadow += 1;
        markLandmarks(child.shadowRoot, frameCategory, true, frameCategory !== topKind);
        let innerFrames = [];
        try { innerFrames = child.shadowRoot.querySelectorAll("iframe"); } catch (e) { innerFrames = []; }
        for (const iframe of innerFrames) considerFrame(iframe, frameCategory, 1);
        visit(child.shadowRoot, depth + 1, frameCategory, true);
      }
      const signature = child.tagName + "|" + classesOf(child).join(".");
      if (!buckets.has(signature)) buckets.set(signature, []);
      buckets.get(signature).push(child);
      visit(child, depth + 1, frameCategory, inShadow);
    }
    for (const rows of buckets.values()) {
      if (rows.length < 2 || classesOf(rows[0]).length === 0) continue;
      const visible = rows.filter((row) => !isHidden(row));
      const hiddenCount = rows.length - visible.length;
      for (const row of rows) seenRows.add(row);
      const seenIds = new Map();
      const idShapes = { generic_shell: 0, other_shape: 0, long_numeric: 0, long: 0, empty: 0 };
      let present = 0;
      let duplicated = 0;
      for (const row of visible) {
        const idValue = row.getAttribute("id");
        if (!idValue) { idShapes.empty += 1; continue; }
        present += 1;
        idShapes[idShape(idValue)] += 1;
        seenIds.set(idValue, (seenIds.get(idValue) || 0) + 1);
      }
      for (const count of seenIds.values()) if (count > 1) duplicated += 1;
      let uniqueness = "insufficient";
      if (present === 0) uniqueness = "not_on_repeated_rows";
      else if (idShapes.generic_shell === present) uniqueness = "generic_shell";
      else if (duplicated > 0) uniqueness = "duplicated";
      else if (seenIds.size === present && present >= 2) uniqueness = "unique_in_this_snapshot";
      const time = { format_description: 0, parseable_time: 0, clock_without_date: 0, other_redacted: 0, empty: 0 };
      const labels = { buyer: 0, merchant: 0, replied: 0, unreplied: 0, other_redacted: 0, empty: 0 };
      for (const row of visible) {
        bump(time, timeKind(row.getAttribute("msgtimeformate")));
        bump(labels, labelKind(row.getAttribute("title")));
        bump(labels, labelKind(row.getAttribute("aria-label")));
      }
      const declaredRaw = parent.getAttribute && parent.getAttribute("aria-rowcount");
      const declared = declaredRaw && /^\\d{1,6}$/.test(declaredRaw) ? Number(declaredRaw) : null;
      groups.push({
        tag: String(rows[0].tagName || "").toLowerCase(),
        class_structure: classesOf(rows[0]),
        path: classesOf(parent).slice(0, 3),
        rendered_count: rows.length,
        visible_count: visible.length,
        hidden_count: hiddenCount,
        virtual_list: declared !== null && declared > rows.length,
        declared_row_count: declared,
        region: regionOf(rows[0]),
        frame_category: frameCategory,
        id: {
          present, distinct: seenIds.size, duplicated, shapes: idShapes,
          uniqueness, stability: "unproven_single_snapshot"
        },
        time, labels
      });
    }
  };
  const shellWalk = (el) => {
    if (!el || visits > VISIT_LIMIT) { reasons.add("visit_limit"); return; }
    if (el.nodeType === 1 && !seenRows.has(el)) {
      const idValue = el.getAttribute && el.getAttribute("id");
      if (idValue) shell[idShape(idValue)] += 1;
    }
    for (const child of childElements(el)) {
      visits += 1;
      shellWalk(child);
    }
  };
  const considerFrame = (iframe, parentKind, nesting) => {
    if (!iframe || processedFrames.has(iframe)) return;
    processedFrames.add(iframe);
    if (nesting > FRAME_LIMIT) {
      recordFrame("off_scope", "frame_nesting_limit", false, nesting);
      reasons.add("frame_nesting_limit");
      return;
    }
    let doc = null;
    try { doc = iframe.contentDocument; } catch (e) {
      recordFrame("unreadable", "access_denied", false, nesting);
      reasons.add("unreadable_frame");
      return;
    }
    if (!doc) {
      recordFrame("unreadable", "no_document", false, nesting);
      reasons.add("unreadable_frame");
      return;
    }
    let host = "", path = "", protocol = "", origin = "";
    try {
      host = String(doc.location.hostname || "").toLowerCase();
      path = String(doc.location.pathname || "").toLowerCase();
      protocol = String(doc.location.protocol || "");
      origin = String(doc.location.origin || "");
    } catch (e) {
      recordFrame("unreadable", "access_denied", false, nesting);
      reasons.add("unreadable_frame");
      return;
    }
    let passwordField = false;
    try { passwordField = !!doc.querySelector("input[type='password']"); }
    catch (e) { passwordField = true; }
    if (passwordField || /login|passport|password|signin/.test(path)) {
      recordFrame("login_or_credential", passwordField ? "password_field" : "url_path", false, nesting);
      return;
    }
    if (/pay|cashier|checkout|wallet/.test(path)) {
      recordFrame("payment", "url_path", false, nesting);
      return;
    }
    const parentOrigin = String(location.origin || "");
    const blank = protocol === "about:";
    const chat = host === "mms.pinduoduo.com" && (path === "/chat-merchant" || path.startsWith("/chat-merchant/"));
    const localParent = parentKind === "local_test" || parentKind === "blank_nested" || parentKind === "chat_merchant" || parentKind === "same_origin_chat";
    const localChild = (host === "127.0.0.1" || host === "localhost") && origin === parentOrigin && !path.startsWith("/off-scope/");
    let category = "";
    if (chat && origin === parentOrigin) category = "same_origin_chat";
    else if (blank && localParent) category = "blank_nested";
    else if ((parentKind === "local_test" || parentKind === "blank_nested") && localChild) category = "local_test";
    if (!category) {
      recordFrame("off_scope", "not_chat_merchant_frame", false, nesting);
      return;
    }
    recordFrame(category, "entered", true, nesting);
    explore(doc, category, nesting);
  };
  const explore = (doc, kind, nesting) => {
    if (!doc || seenDocs.has(doc)) return;
    seenDocs.add(doc);
    const root = doc.body || doc.documentElement;
    if (!root) return;
    const inFrame = nesting > 0;
    markLandmarks(doc, kind, false, inFrame);
    let iframes = [];
    try { iframes = doc.querySelectorAll("iframe"); } catch (e) { iframes = []; }
    for (const iframe of iframes) considerFrame(iframe, kind, nesting + 1);
    const shadowHosts = [];
    try { shadowHosts.push(...doc.querySelectorAll("*")); } catch (e) { shadowHosts.length = 0; }
    for (const el of shadowHosts) {
      if (visits >= VISIT_LIMIT) { reasons.add("shadow_visit_limit"); break; }
      visits += 1;
      if (!el.shadowRoot || seenShadows.has(el.shadowRoot)) continue;
      seenShadows.add(el.shadowRoot);
      openShadow += 1;
      markLandmarks(el.shadowRoot, kind, true, inFrame);
      let innerFrames = [];
      try { innerFrames = el.shadowRoot.querySelectorAll("iframe"); } catch (e) { innerFrames = []; }
      for (const iframe of innerFrames) considerFrame(iframe, kind, nesting + 1);
      visit(el.shadowRoot, 1, kind, true);
    }
    visit(root, 0, kind, false);
    shellWalk(root);
  };

  if (topKind === "chat_merchant" || topKind === "local_test") explore(document, topKind, 0);
  else recordFrame(topKind, "top_page", false, 0);

  const exported = new Set();
  const exportLandmark = (item) => {
    exportTree(item.el, item.region === "history_control" ? "unspecified" : item.region, item.frame_category, item.in_shadow, 0);
  };
  for (const item of landmarkEls) if (item.region === "message_container") exportLandmark(item);
  for (const item of landmarkEls) if (item.region === "conversation_list") exportLandmark(item);
  if (!nodes.length && document.body) snapshotNode(document.body, "unspecified", topKind, false, 0);

  const messageHits = landmarkEls.filter((item) => item.names.some((name) => MESSAGE.has(name)));
  const listHits = landmarkEls.filter((item) => item.names.some((name) => LIST.has(name)));
  const historyHits = landmarkEls.filter((item) => item.names.some((name) => HISTORY.has(name)));
  const incomplete = reasons.size > 0;
  const timeShapeOf = (value) => {
    const text = String(value || "").replace(/\\s+/g, " ").trim();
    if (!text) return "empty";
    if (text.length > 60) return "other_redacted";
    const date = /\\d{4}[-/.年]\\s?\\d{1,2}[-/.月]\\s?\\d{1,2}/.test(text) || /\\d{1,2}月\\d{1,2}日/.test(text);
    const clock = /\\d{1,2}:\\d{2}/.test(text);
    if (date && clock) return "date_and_time";
    if (date && text.length <= 24) return "date_only";
    if (/^\\d{1,2}:\\d{2}(?::\\d{2})?$/.test(text)) return "clock_without_date";
    if (clock) return "clock_with_other_text";
    return "other_redacted";
  };
  const classWithinRow = (row, name) => {
    if (classesOf(row).includes(name)) return row;
    let found = [];
    try { found = row.querySelectorAll("." + name); } catch (e) { return null; }
    for (const el of found) {
      const owner = el.closest ? el.closest("li.onemsg") : null;
      if (owner === row) return el;
    }
    return null;
  };
  const describeRow = (row) => {
    const merchant = classWithinRow(row, "merchantMessage");
    const buyer = classWithinRow(row, "buyer-item");
    let buyerRelation = "absent";
    if (buyer) {
      if (merchant && merchant.contains(buyer)) buyerRelation = "inside_merchant_message";
      else if (merchant && buyer.contains(merchant)) buyerRelation = "ancestor_of_merchant_message";
      else if (merchant && merchant.parentElement && buyer.parentElement === merchant.parentElement) buyerRelation = "sibling_of_merchant_message";
      else buyerRelation = "elsewhere_in_row";
    }
    const timeEl = classWithinRow(row, "message-time");
    let header = "absent";
    let prev = row.previousElementSibling;
    let guard = 0;
    while (prev && guard < 6) {
      if (classesOf(prev).includes("onemsg")) break;
      const shape = timeShapeOf(prev.textContent);
      if (shape === "date_only" || shape === "date_and_time") { header = "present"; break; }
      prev = prev.previousElementSibling;
      guard += 1;
    }
    const bodyEl = classWithinRow(row, "msg-content-box") || classWithinRow(row, "msg-content");
    const attrNames = [];
    for (const attr of Array.from(row.attributes || [])) {
      const name = String(attr.name || "");
      if (name === "tabindex" || name.indexOf("data-v-") === 0) continue;
      if (/^[A-Za-z_:][A-Za-z0-9_:-]{0,40}$/.test(name) && attrNames.indexOf(name) < 0) attrNames.push(name);
    }
    return {
      idValue: row.getAttribute("id") || "",
      textKey: bodyEl ? String(bodyEl.textContent || "").replace(/\\s+/g, " ").trim() : "",
      hidden: isHidden(row),
      merchant: !!merchant,
      buyerRelation,
      timeShape: timeEl ? timeShapeOf(timeEl.textContent) : "absent",
      header,
      readMark: classWithinRow(row, "isread") ? "present" : "absent",
      attrNames
    };
  };
  const scanRows = () => {
    const rows = [];
    const seen = new Set();
    let boxes = 0;
    let truncated = false;
    const roots = [];
    for (const doc of seenDocs) roots.push(doc);
    for (const shadow of seenShadows) roots.push(shadow);
    for (const root of roots) {
      let boxesFound = [];
      try { boxesFound = root.querySelectorAll ? root.querySelectorAll(".content-box") : []; }
      catch (e) { boxesFound = []; }
      for (const box of boxesFound) {
        if (seen.has(box)) continue;
        seen.add(box);
        boxes += 1;
        let found = [];
        try { found = box.querySelectorAll("li.onemsg"); } catch (e) { found = []; }
        for (const row of found) {
          if (seen.has(row)) continue;
          if (rows.length >= 200) { truncated = true; break; }
          seen.add(row);
          rows.push(describeRow(row));
        }
      }
    }
    return { rows, boxes, truncated };
  };
  const summarize = (pass, stability) => {
    const visible = pass.rows.filter((row) => !row.hidden);
    const hidden = pass.rows.length - visible.length;
    const withMerchant = visible.filter((row) => row.merchant).length;
    let merchantRelation = "absent";
    if (visible.length && withMerchant === visible.length) merchantRelation = "covers_every_visible_row";
    else if (withMerchant > 0) merchantRelation = "covers_some_rows";
    const buyer = { absent: 0, inside_merchant_message: 0, ancestor_of_merchant_message: 0, sibling_of_merchant_message: 0, elsewhere_in_row: 0 };
    const times = {};
    let headerPresent = 0;
    let readPresent = 0;
    const patterns = new Map();
    const attrNames = new Set();
    const idShapes = { generic_shell: 0, other_shape: 0, long_numeric: 0, long: 0, empty: 0 };
    const visibleIds = new Map();
    const textIds = new Map();
    for (const row of visible) {
      buyer[row.buyerRelation] = (buyer[row.buyerRelation] || 0) + 1;
      times[row.timeShape] = (times[row.timeShape] || 0) + 1;
      if (row.header === "present") headerPresent += 1;
      if (row.readMark === "present") readPresent += 1;
      idShapes[idShape(row.idValue)] += 1;
      if (row.idValue) visibleIds.set(row.idValue, (visibleIds.get(row.idValue) || 0) + 1);
      if (row.textKey && row.idValue) {
        if (!textIds.has(row.textKey)) textIds.set(row.textKey, new Set());
        textIds.get(row.textKey).add(row.idValue);
      }
      for (const name of row.attrNames) attrNames.add(name);
      const key = [row.merchant ? "merchant" : "no_merchant", row.buyerRelation, row.timeShape, row.header, row.readMark].join("|");
      patterns.set(key, (patterns.get(key) || 0) + 1);
    }
    let sameTextDifferentId = 0;
    for (const ids of textIds.values()) if (ids.size > 1) sameTextDifferentId += ids.size;
    let duplicatedVisibleIds = 0;
    for (const count of visibleIds.values()) if (count > 1) duplicatedVisibleIds += 1;
    const visibleIdSet = new Set(visibleIds.keys());
    let hiddenCollides = 0;
    for (const row of pass.rows) if (row.hidden && row.idValue && visibleIdSet.has(row.idValue)) hiddenCollides += 1;
    const presentTimes = Object.keys(times).filter((key) => key !== "absent" && times[key]);
    let timeEvidence = "absent";
    if (presentTimes.length === 1 && presentTimes[0] === "clock_without_date" && headerPresent > 0) timeEvidence = "clock_with_adjacent_date_header";
    else if (presentTimes.length === 1 && presentTimes[0] === "clock_without_date") timeEvidence = "clock_without_date";
    else if (presentTimes.length === 1 && presentTimes[0] === "date_and_time") timeEvidence = "parseable_shape_on_row_meaning_unconfirmed";
    else if (presentTimes.length > 0) timeEvidence = "mixed_or_unreadable";
    const patternRows = [];
    for (const [key, count] of patterns) {
      const parts = key.split("|");
      patternRows.push({
        visible: count,
        merchant_message: parts[0] === "merchant" ? "present" : "absent",
        buyer_item: parts[1],
        time_shape: parts[2],
        adjacent_date_header: parts[3],
        read_mark: parts[4],
        reply_status: "read_unknown"
      });
    }
    return {
      content_boxes: pass.boxes,
      rows_seen: pass.rows.length,
      visible_rows: visible.length,
      hidden_rows: hidden,
      truncated: pass.truncated,
      row_boundary: { tag: "li", class_name: "onemsg", validated: false },
      chat_item_counts_as_message_region: false,
      merchant_message: { visible_rows_containing: withMerchant, relation: merchantRelation, used_as_direction: false },
      buyer_item: buyer,
      direction_evidence: "insufficient_name_not_used",
      time_shapes: times,
      rows_with_adjacent_date_header: headerPresent,
      time_evidence: timeEvidence,
      can_treat_time_as_message_time: false,
      read_mark_rows: readPresent,
      reply_evidence: "read_unknown",
      attribute_names: Array.from(attrNames).slice(0, 12),
      id_shapes: idShapes,
      duplicated_visible_ids: duplicatedVisibleIds,
      same_text_different_id_rows: sameTextDifferentId,
      hidden_id_collides_with_visible: hiddenCollides,
      patterns: patternRows.slice(0, 12),
      id_stability: stability,
      body_retained: false,
      values_included: false,
      production_mapping_validated: false
    };
  };
  const comparePasses = (first, second) => {
    const av = first.rows.filter((row) => !row.hidden);
    const bv = second.rows.filter((row) => !row.hidden);
    let same = 0;
    let changed = 0;
    let missing = 0;
    const limit = Math.min(av.length, bv.length);
    for (let index = 0; index < limit; index += 1) {
      if (!av[index].idValue || !bv[index].idValue) { missing += 1; continue; }
      if (av[index].idValue === bv[index].idValue) same += 1;
      else changed += 1;
    }
    let samePage = "incomplete";
    if (av.length === bv.length && missing === 0 && changed === 0 && same === av.length && av.length > 0) samePage = "unchanged";
    else if (changed > 0 || av.length !== bv.length) samePage = "changed";
    return {
      observations: 2,
      scope: "same_page_two_frames",
      cross_restart: "unproven",
      visible_first: av.length,
      visible_second: bv.length,
      same_id_at_same_position: same,
      id_changed_at_same_position: changed,
      positions_without_id: missing,
      same_page_result: samePage,
      can_use_for_dedupe: false
    };
  };
  const buildReport = (firstPass, secondPass) => {
    return {
    nodes,
    truncated: incomplete,
    truncation_reasons: Array.from(reasons),
    coverage: {
      top_page: topKind,
      depth_limit: DEPTH_LIMIT,
      visit_limit: VISIT_LIMIT,
      sample_depth: SAMPLE_DEPTH,
      max_depth_reached: maxDepth,
      elements_visited: visits,
      omitted_by_depth: omittedByDepth,
      sample_omitted: sampleOmitted,
      open_shadow_roots_entered: openShadow,
      closed_shadow_not_observable: true,
      incomplete,
      reasons: Array.from(reasons)
    },
    frames: { items: frames },
    message_region: {
      reached: messageHits.length > 0,
      hidden_only: messageHits.length > 0 && messageHits.every((item) => item.hidden),
      in_frame: messageHits.some((item) => item.in_frame === true),
      in_open_shadow: messageHits.some((item) => item.in_shadow),
      landmarks: messageHits.reduce((bag, item) => bag.concat(item.names.filter((name) => MESSAGE.has(name))), []),
      conversation_list_landmarks: listHits.reduce((bag, item) => bag.concat(item.names.filter((name) => LIST.has(name))), []),
      history_controls: historyHits.reduce((bag, item) => bag.concat(item.names.filter((name) => HISTORY.has(name))), []),
      max_landmark_depth: messageHits.reduce((deepest, item) => Math.max(deepest, item.depth), 0),
      conversation_list_counted_as_messages: false,
      chat_item_counts_as_message_region: false
    },
    message_preflight: summarize(secondPass, comparePasses(firstPass, secondPass)),
    value_assessment: { groups, shell_ids: shell }
  };
  };
  const firstPass = scanRows();
  return new Promise((resolve) => {
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      resolve(buildReport(firstPass, scanRows()));
    };
    if (typeof requestAnimationFrame === "function") requestAnimationFrame(finish);
    setTimeout(finish, 50);
  });
}
"""


def _unique(tokens: list[str]) -> list[str]:
    seen: list[str] = []
    for token in tokens:
        if token not in seen:
            seen.append(token)
    return seen


def _apply_scope(report: dict[str, Any], raw: dict[str, Any]) -> None:
    """Attach frame and coverage facts. This cannot validate a production mapping."""
    coverage_in = raw.get("coverage") if isinstance(raw.get("coverage"), dict) else None
    frames_in = raw.get("frames") if isinstance(raw.get("frames"), dict) else None
    region_in = raw.get("message_region") if isinstance(raw.get("message_region"), dict) else None
    if coverage_in is None and frames_in is None and region_in is None:
        report["coverage"] = {"source": "not_provided", "incomplete": False, "reasons": []}
        report["frames"] = {"seen": 0, "entered": 0, "skipped": 0, "by_category": {}}
        report["message_region"] = {
            "reached": False,
            "reason": "not_provided",
            "conversation_list_counted_as_messages": False,
        }
        report["production_mapping_validated"] = False
        report["collection_enabled"] = False
        report["selectors_inferred"] = False
        report["platform_unsupported"] = False
        return
    reasons = [
        reason
        for reason in (coverage_in or {}).get("reasons") or raw.get("truncation_reasons") or []
        if reason in _TRUNCATION_REASONS
    ]
    reasons = _unique([str(reason) for reason in reasons])
    incomplete = bool(reasons)
    items_in = frames_in.get("items") if isinstance(frames_in, dict) else None
    items: list[dict[str, Any]] = []
    by_category: dict[str, int] = {}
    if isinstance(items_in, list):
        for item in items_in[:40]:
            if not isinstance(item, dict):
                continue
            category = item.get("category")
            reason = item.get("reason")
            if category not in _FRAME_CATEGORIES:
                category = "off_scope"
            if reason not in _FRAME_REASONS:
                reason = "not_chat_merchant_frame"
            entered = item.get("entered") is True and category in {
                "same_origin_chat",
                "local_test",
                "blank_nested",
            }
            items.append(
                {
                    "category": category,
                    "reason": reason,
                    "entered": entered,
                    "nesting": _clean_int(item.get("nesting")),
                }
            )
            by_category[category] = by_category.get(category, 0) + 1
    entered = sum(1 for item in items if item["entered"])
    message_names = [
        name
        for name in (region_in or {}).get("landmarks") or []
        if name in _MESSAGE_LANDMARKS
    ]
    list_names = [
        name
        for name in (region_in or {}).get("conversation_list_landmarks") or []
        if name in _LIST_LANDMARKS
    ]
    history_names = [
        name
        for name in (region_in or {}).get("history_controls") or []
        if name in _HISTORY_LANDMARKS
    ]
    reached = bool(message_names) and (region_in or {}).get("reached") is not False
    top_page = (coverage_in or {}).get("top_page")
    if top_page not in {"chat_merchant", "local_test", "login_or_credential", "payment", "off_scope"}:
        top_page = "off_scope"
    if reached:
        region_reason = "observed"
    elif top_page in {"login_or_credential", "payment", "off_scope"}:
        region_reason = "top_page_off_scope"
    elif incomplete:
        region_reason = (
            "conversation_list_observed_message_region_not_sampled"
            if list_names
            else "not_in_sampled_range"
        )
    else:
        region_reason = "not_observed_within_bounds"
    report["coverage"] = {
        "source": "in_page",
        "top_page": top_page,
        "depth_limit": _clean_int((coverage_in or {}).get("depth_limit")) or None,
        "visit_limit": _clean_int((coverage_in or {}).get("visit_limit")) or None,
        "sample_depth": _clean_int((coverage_in or {}).get("sample_depth")) or None,
        "max_depth_reached": _clean_int((coverage_in or {}).get("max_depth_reached")),
        "elements_visited": _clean_int((coverage_in or {}).get("elements_visited")),
        "omitted_by_depth": _clean_int((coverage_in or {}).get("omitted_by_depth")),
        "sample_omitted": _clean_int((coverage_in or {}).get("sample_omitted")),
        "open_shadow_roots_entered": _clean_int((coverage_in or {}).get("open_shadow_roots_entered")),
        "closed_shadow_not_observable": True,
        "incomplete": incomplete,
        "reasons": reasons,
    }
    report["frames"] = {
        "seen": len(items),
        "entered": entered,
        "skipped": len(items) - entered,
        "by_category": by_category,
        "items": items,
    }
    report["message_region"] = {
        "reached": reached,
        "reason": region_reason,
        "hidden_only": reached and (region_in or {}).get("hidden_only") is True,
        "in_frame": reached and (region_in or {}).get("in_frame") is True,
        "in_open_shadow": reached and (region_in or {}).get("in_open_shadow") is True,
        "landmarks": _unique(message_names),
        "conversation_list_landmarks": _unique(list_names),
        "history_controls": _unique(history_names),
        "max_landmark_depth": _clean_int((region_in or {}).get("max_landmark_depth")),
        "conversation_list_counted_as_messages": False,
        "chat_item_counts_as_message_region": False,
        "structure_exported": any(
            node.get("region") == "message_container" for node in report.get("nodes") or []
        ),
    }
    report["truncated"] = bool(report.get("truncated") or incomplete)
    report["production_mapping_validated"] = False
    report["collection_enabled"] = False
    report["selectors_inferred"] = False
    report["selector_candidates"] = []
    report["platform_unsupported"] = False
    if report.get("status") in {"conversation_not_selected", "structure_observed", "diagnostic_error"}:
        return
    if reached:
        report["status"] = "identity_unproven"
        report["reason"] = "message_region_observed_identity_unproven"
    elif incomplete or top_page in {"login_or_credential", "payment", "off_scope"}:
        report["status"] = "coverage_incomplete"
        report["reason"] = region_reason


_PREFLIGHT_RELATIONS = frozenset(
    {
        "absent",
        "covers_every_visible_row",
        "covers_some_rows",
        "inside_merchant_message",
        "ancestor_of_merchant_message",
        "sibling_of_merchant_message",
        "elsewhere_in_row",
    }
)
_PREFLIGHT_SHAPES = frozenset(
    {
        "empty",
        "absent",
        "date_and_time",
        "date_only",
        "clock_without_date",
        "clock_with_other_text",
        "other_redacted",
        "present",
    }
)
_PREFLIGHT_TIME = frozenset(
    {
        "absent",
        "clock_with_adjacent_date_header",
        "clock_without_date",
        "parseable_shape_on_row_meaning_unconfirmed",
        "mixed_or_unreadable",
    }
)
_PREFLIGHT_STABILITY = frozenset({"unchanged", "changed", "incomplete"})
_ATTR_NAME = re.compile(r"^[A-Za-z_:][A-Za-z0-9_:-]{0,40}$")


def _clean_preflight(raw: Any) -> dict[str, Any]:
    """Keep counts and enums. Drop raw ids, bodies, dates, and URLs."""
    if not isinstance(raw, dict):
        return {
            "source": "not_provided",
            "visible_rows": 0,
            "direction_evidence": "insufficient_name_not_used",
            "reply_evidence": "read_unknown",
            "can_treat_time_as_message_time": False,
            "chat_item_counts_as_message_region": False,
            "body_retained": False,
            "values_included": False,
            "production_mapping_validated": False,
        }
    buyer_in = raw.get("buyer_item") if isinstance(raw.get("buyer_item"), dict) else {}
    buyer = {
        key: _clean_int(buyer_in.get(key))
        for key in (
            "absent",
            "inside_merchant_message",
            "ancestor_of_merchant_message",
            "sibling_of_merchant_message",
            "elsewhere_in_row",
        )
    }
    shapes_in = raw.get("time_shapes") if isinstance(raw.get("time_shapes"), dict) else {}
    time_shapes = {key: _clean_int(shapes_in.get(key)) for key in _PREFLIGHT_SHAPES}
    ids_in = raw.get("id_shapes") if isinstance(raw.get("id_shapes"), dict) else {}
    id_shapes = {
        key: _clean_int(ids_in.get(key))
        for key in ("generic_shell", "other_shape", "long_numeric", "long", "empty")
    }
    merchant_in = raw.get("merchant_message") if isinstance(raw.get("merchant_message"), dict) else {}
    relation = merchant_in.get("relation")
    stability_in = raw.get("id_stability") if isinstance(raw.get("id_stability"), dict) else {}
    same_page = stability_in.get("same_page_result")
    patterns = []
    for item in raw.get("patterns") or []:
        if not isinstance(item, dict) or len(patterns) >= 12:
            continue
        buyer_item = item.get("buyer_item")
        time_shape = item.get("time_shape")
        header = item.get("adjacent_date_header")
        read_mark = item.get("read_mark")
        merchant = item.get("merchant_message")
        patterns.append(
            {
                "visible": _clean_int(item.get("visible")),
                "merchant_message": merchant if merchant in {"present", "absent"} else "absent",
                "buyer_item": buyer_item if buyer_item in _PREFLIGHT_RELATIONS else "absent",
                "time_shape": time_shape if time_shape in _PREFLIGHT_SHAPES else "other_redacted",
                "adjacent_date_header": header if header in {"present", "absent"} else "absent",
                "read_mark": read_mark if read_mark in {"present", "absent"} else "absent",
                "reply_status": "read_unknown",
            }
        )
    names = [
        name
        for name in raw.get("attribute_names") or []
        if isinstance(name, str) and _ATTR_NAME.fullmatch(name) and not name.startswith("data-v-")
    ]
    return {
        "source": "in_page",
        "content_boxes": _clean_int(raw.get("content_boxes")),
        "rows_seen": _clean_int(raw.get("rows_seen")),
        "visible_rows": _clean_int(raw.get("visible_rows")),
        "hidden_rows": _clean_int(raw.get("hidden_rows")),
        "truncated": raw.get("truncated") is True,
        "row_boundary": {"tag": "li", "class_name": "onemsg", "validated": False},
        "chat_item_counts_as_message_region": False,
        "merchant_message": {
            "visible_rows_containing": _clean_int(merchant_in.get("visible_rows_containing")),
            "relation": relation if relation in _PREFLIGHT_RELATIONS else "absent",
            "used_as_direction": False,
        },
        "buyer_item": buyer,
        "direction_evidence": "insufficient_name_not_used",
        "time_shapes": time_shapes,
        "rows_with_adjacent_date_header": _clean_int(raw.get("rows_with_adjacent_date_header")),
        "time_evidence": raw.get("time_evidence") if raw.get("time_evidence") in _PREFLIGHT_TIME else "absent",
        "can_treat_time_as_message_time": False,
        "read_mark_rows": _clean_int(raw.get("read_mark_rows")),
        "reply_evidence": "read_unknown",
        "attribute_names": names[:12],
        "id_shapes": id_shapes,
        "duplicated_visible_ids": _clean_int(raw.get("duplicated_visible_ids")),
        "same_text_different_id_rows": _clean_int(raw.get("same_text_different_id_rows")),
        "hidden_id_collides_with_visible": _clean_int(raw.get("hidden_id_collides_with_visible")),
        "patterns": patterns,
        "id_stability": {
            "observations": 2 if _clean_int(stability_in.get("observations")) == 2 else 0,
            "scope": "same_page_two_frames",
            "cross_restart": "unproven",
            "visible_first": _clean_int(stability_in.get("visible_first")),
            "visible_second": _clean_int(stability_in.get("visible_second")),
            "same_id_at_same_position": _clean_int(stability_in.get("same_id_at_same_position")),
            "id_changed_at_same_position": _clean_int(stability_in.get("id_changed_at_same_position")),
            "positions_without_id": _clean_int(stability_in.get("positions_without_id")),
            "same_page_result": same_page if same_page in _PREFLIGHT_STABILITY else "incomplete",
            "can_use_for_dedupe": False,
        },
        "body_retained": False,
        "values_included": False,
        "production_mapping_validated": False,
    }


def export_rendered_dom(page: Any, *, timeout_seconds: float = 2.0) -> dict[str, Any]:
    """Export structure from an already-open page. It does not call goto or click."""
    if timeout_seconds <= 0:
        return _error("timeout_invalid")
    try:
        page.set_default_timeout(int(timeout_seconds * 1000))
        raw = page.evaluate(_EXPORT_JS)
    except Exception:
        return _error("evaluate_failed")
    if not isinstance(raw, dict):
        return _error("evaluate_invalid")
    if raw.get("error"):
        return _error(str(raw.get("error")))
    nodes = raw.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return _error("no_rendered_elements")
    report = _classify(nodes, truncated=bool(raw.get("truncated")))
    report["value_assessment"] = _value_decisions(raw.get("value_assessment"))
    _apply_scope(report, raw)
    report["message_preflight"] = _clean_preflight(raw.get("message_preflight"))
    preflight = report["message_preflight"]
    if (
        preflight.get("visible_rows", 0) > 0
        and report.get("status") not in {"conversation_not_selected", "structure_observed", "diagnostic_error"}
    ):
        report["status"] = "identity_unproven"
        report["reason"] = "message_rows_observed_direction_time_reply_unconfirmed"
    report["production_mapping_validated"] = False
    report["collection_enabled"] = False
    report["selectors_inferred"] = False
    return report


def service_diagnose_request(page: Any, data_dir: Path, *, timeout_seconds: float = 2.0) -> None:
    """If the owning process sees a request file, write one report and keep the page."""
    request = data_dir / "dom-diagnose.request"
    if not request.is_file():
        return
    try:
        request.unlink()
    except OSError:
        return
    result = export_rendered_dom(page, timeout_seconds=timeout_seconds)
    target = data_dir / "dom-diagnose.result.json"
    temporary = data_dir / ".dom-diagnose.result.tmp"
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)
    os.chmod(target, 0o600)


def wait_for_diagnose_result(data_dir: Path, *, timeout_seconds: float) -> dict[str, Any]:
    """Ask the already-running owner to export. This function does not launch a browser."""
    import time

    result_path = data_dir / "dom-diagnose.result.json"
    if result_path.exists():
        result_path.unlink()
    request = data_dir / "dom-diagnose.request"
    request.write_text("{}\n", encoding="utf-8")
    os.chmod(request, 0o600)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if result_path.is_file():
            try:
                payload = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return _error("result_unreadable")
            if isinstance(payload, dict):
                return payload
            return _error("result_invalid")
        time.sleep(0.05)
    return _error("listener_not_running")


def _classify(nodes: list[Any], *, truncated: bool = False) -> dict[str, Any]:
    cleaned: list[dict[str, Any]] = []
    ignored: set[str] = set()
    conversation_flag_seen = False
    conversation_selected = None
    fixture_names_present = False
    shell_classes: set[str] = set()
    attribute_names: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict):
            continue
        names = [str(name) for name in node.get("attributes") or [] if isinstance(name, str)]
        kept_names: list[str] = []
        for name in names:
            if name == "tabindex" or name.startswith("data-v-"):
                ignored.add(name if name == "tabindex" else "data-v-")
                continue
            kept_names.append(name)
            attribute_names.add(name)
            if name in _FIXTURE_NAMES:
                fixture_names_present = True
        selected = node.get("conversation_selected")
        if selected is True or selected is False:
            conversation_flag_seen = True
            conversation_selected = selected
        class_structure = [
            token
            for token in node.get("class_structure") or []
            if isinstance(token, str) and _SAFE_CLASS.fullmatch(token)
        ]
        path = [
            token
            for token in node.get("path") or []
            if isinstance(token, str) and _SAFE_CLASS.fullmatch(token)
        ]
        shell_classes.update(token for token in class_structure if token in _OBSERVED_SHELL_CLASSES)
        region = node.get("region")
        frame_category = node.get("frame_category")
        cleaned.append(
            {
                "tag": str(node.get("tag") or ""),
                "attributes": kept_names,
                "class_structure": class_structure,
                "path": path,
                "depth": int(node.get("depth") or 0),
                "child_element_count": int(node.get("child_element_count") or 0),
                "region": region if region in {"message_container", "conversation_list", "unspecified"} else "unspecified",
                "frame_category": frame_category if frame_category in _FRAME_CATEGORIES or frame_category in {"chat_merchant", "local_test"} else "unspecified",
                "in_shadow": node.get("in_shadow") is True,
                "hidden": node.get("hidden") is True,
            }
        )
    if not cleaned:
        return _error("no_rendered_elements")
    repeated = _repeated_rows(cleaned)
    identity = _identity_report(attribute_names, fixture_names_present, shell_classes)
    merchant_hint = bool(shell_classes or attribute_names & (_TIME_ATTRIBUTE_NAMES | {"data-clipboard-text"}))
    if conversation_flag_seen and conversation_selected is False:
        status = "conversation_not_selected"
        reason = "conversation_flag_false"
    elif fixture_names_present:
        status = "structure_observed"
        reason = "fixture_semantic_names_present"
    elif merchant_hint:
        status = "identity_unproven"
        reason = "merchant_structure_present_identity_unproven"
    else:
        status = "fields_absent"
        reason = "no_known_message_structure"
    return {
        "status": status,
        "reason": reason,
        "nodes": cleaned,
        "repeated_structures": repeated,
        "identity": identity,
        "observed_shell_classes": sorted(shell_classes),
        "fixture_names_required": False,
        "ignored_unstable_attributes": sorted(ignored),
        "selector_candidates": [],
        "selectors_inferred": False,
        "production_mapping_validated": False,
        "collection_enabled": False,
        "truncated": truncated,
        "platform_unsupported": False,
        "text_included": False,
        "values_included": False,
        "real_store_connected": False,
    }


def _repeated_rows(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[tuple[str, tuple[str, ...], tuple[str, ...]], int] = {}
    for node in nodes:
        key = (
            str(node["tag"]),
            tuple(node["class_structure"]),
            tuple(node["path"][-3:]),
        )
        counts[key] = counts.get(key, 0) + 1
    repeated = []
    for (tag, classes, path), count in counts.items():
        if count < 2 or not classes:
            continue
        repeated.append(
            {
                "tag": tag,
                "class_structure": list(classes),
                "path": list(path),
                "count": count,
                "row_identity": "unproven",
            }
        )
    repeated.sort(key=lambda item: (-int(item["count"]), str(item["class_structure"])))
    return repeated[:20]


def _identity_report(
    attribute_names: set[str],
    fixture_names_present: bool,
    shell_classes: set[str],
) -> dict[str, str]:
    return {
        "stable_message_id": "unproven",
        "direction": "present_as_fixture_name" if "data-direction" in attribute_names else "unproven",
        "message_time": "unproven",
        "reply_status": "unproven",
        "fixture_attribute_names": "present" if fixture_names_present else "absent",
        "id_attribute_name": "present_value_unread" if "id" in attribute_names else "absent",
        "time_attribute_name": (
            "present_value_unread"
            if attribute_names & _TIME_ATTRIBUTE_NAMES
            else "absent"
        ),
        "text_carrier_names": (
            "present_value_unread"
            if attribute_names & _TEXT_CARRIER_NAMES
            else "absent"
        ),
        "merchant_shell": "present" if shell_classes else "absent",
    }


_ENUMS = frozenset(
    {
        "unique_in_this_snapshot",
        "duplicated",
        "not_on_repeated_rows",
        "generic_shell",
        "insufficient",
        "unproven_single_snapshot",
        "other_shape",
        "long_numeric",
        "long",
        "empty",
        "format_description",
        "parseable_time",
        "clock_without_date",
        "other_redacted",
        "buyer",
        "merchant",
        "replied",
        "unreplied",
    }
)


def _clean_token(value: Any) -> str | None:
    if isinstance(value, str) and (_SAFE_CLASS.fullmatch(value) or value in _ENUMS):
        return value
    return None


def _clean_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if 0 <= value <= 1_000_000 else 0


def _value_decisions(raw: Any) -> dict[str, Any]:
    groups_in = raw.get("groups") if isinstance(raw, dict) else None
    shell_in = raw.get("shell_ids") if isinstance(raw, dict) else None
    groups: list[dict[str, Any]] = []
    if isinstance(groups_in, list):
        for item in groups_in:
            if not isinstance(item, dict):
                continue
            classes = [token for token in item.get("class_structure") or [] if _clean_token(token)]
            path = [token for token in item.get("path") or [] if _clean_token(token)]
            if not classes:
                continue
            identity = item.get("id") if isinstance(item.get("id"), dict) else {}
            uniqueness = identity.get("uniqueness")
            groups.append(
                {
                    "tag": _clean_token(item.get("tag")) or "unknown",
                    "class_structure": classes,
                    "path": path,
                    "rendered_count": _clean_int(item.get("rendered_count")),
                    "visible_count": _clean_int(item.get("visible_count")),
                    "hidden_count": _clean_int(item.get("hidden_count")),
                    "virtual_list": item.get("virtual_list") is True,
                    "declared_row_count": _clean_int(item.get("declared_row_count")) or None,
                    "id": {
                        "present": _clean_int(identity.get("present")),
                        "distinct": _clean_int(identity.get("distinct")),
                        "duplicated": _clean_int(identity.get("duplicated")),
                        "shapes": {
                            key: _clean_int((identity.get("shapes") or {}).get(key))
                            for key in ("generic_shell", "other_shape", "long_numeric", "long", "empty")
                        },
                        "uniqueness": uniqueness if uniqueness in _ENUMS else "insufficient",
                        "stability": "unproven_single_snapshot",
                    },
                    "time": {
                        key: _clean_int((item.get("time") or {}).get(key))
                        for key in (
                            "format_description",
                            "parseable_time",
                            "clock_without_date",
                            "other_redacted",
                            "empty",
                        )
                    },
                    "labels": {
                        key: _clean_int((item.get("labels") or {}).get(key))
                        for key in ("buyer", "merchant", "replied", "unreplied", "other_redacted", "empty")
                    },
                    "row_identity": "unproven",
                    "region": item.get("region")
                    if item.get("region") in {"message_container", "conversation_list", "unspecified"}
                    else "unspecified",
                }
            )
    shell = {
        key: _clean_int((shell_in or {}).get(key))
        for key in ("generic_shell", "other_shape", "long_numeric", "long")
    }
    time_totals = {key: 0 for key in ("format_description", "parseable_time", "clock_without_date", "other_redacted")}
    label_totals = {key: 0 for key in ("buyer", "merchant", "replied", "unreplied", "other_redacted")}
    hidden = 0
    virtual = False
    unique_groups = 0
    duplicated_groups = 0
    missing_id_groups = 0
    generic_groups = 0
    for group in groups:
        hidden += group["hidden_count"]
        virtual = virtual or group["virtual_list"]
        uniqueness = group["id"]["uniqueness"]
        if uniqueness == "unique_in_this_snapshot":
            unique_groups += 1
        elif uniqueness == "duplicated":
            duplicated_groups += 1
        elif uniqueness == "not_on_repeated_rows":
            missing_id_groups += 1
        elif uniqueness == "generic_shell":
            generic_groups += 1
        for key, value in group["time"].items():
            if key in time_totals:
                time_totals[key] += value
        for key, value in group["labels"].items():
            if key in label_totals:
                label_totals[key] += value
    human: list[str] = []
    if not groups:
        id_evidence = "insufficient_no_repeated_rows"
        human.append("还没有可比较的重复行。请只确认屏幕上哪一块是会话或消息列表，不要复制 id。")
    elif virtual:
        id_evidence = "incomplete_virtual_list"
        human.append("列表像是虚拟滚动，当前只渲染了一部分。不能用这一屏证明 id 唯一。")
    elif duplicated_groups:
        id_evidence = "duplicated"
    elif generic_groups and generic_groups == len(groups):
        id_evidence = "generic_shell_not_message_id"
        human.append("重复行上的 id 是外壳节点常用名，不能当成消息 id。")
    elif missing_id_groups and missing_id_groups == len(groups):
        id_evidence = "not_on_repeated_rows"
        if any(shell.values()):
            human.append("重复行上没有 id。外壳节点上的 id 只说明页面骨架，不能当成消息 id。")
    elif unique_groups and unique_groups == len(groups):
        id_evidence = "unique_in_this_snapshot"
        human.append("这次采样里重复行的 id 没有撞车，但还不能证明换一条消息后仍然稳定。不要把 id 原文发出来。")
    else:
        id_evidence = "insufficient"
        human.append("重复行的 id 形状不一致，或证据不够。不能据此做消息去重。")
    present_times = [key for key, count in time_totals.items() if count]
    if present_times == ["format_description"]:
        time_evidence = "format_description_not_a_time"
    elif present_times == ["parseable_time"]:
        time_evidence = "parseable_shape_needs_human_meaning_check"
        human.append("msgtimeformate 的形状像日期时间。请在屏幕上确认它是不是该条消息的发送时间，不要把原文贴进工具。")
    elif present_times == ["clock_without_date"]:
        time_evidence = "clock_without_date"
        human.append("只看到钟点、没有日期，不能当作完整消息时间。")
    elif present_times == ["other_redacted"]:
        time_evidence = "unreadable_redacted"
        human.append("msgtimeformate 不是格式说明，也不是可解析的日期时间。原文没有写出。")
    elif present_times:
        time_evidence = "mixed_or_unreadable"
        human.append("时间属性里有不止一种形状。不能把它当成消息时间。")
    else:
        time_evidence = "absent"
    if label_totals["other_redacted"]:
        role_evidence = "non_allowlist_text_redacted"
        human.append("title 或 aria-label 含有买家、商家、已回复、未回复以外的文字。请只回答是否恰好是这四个词之一。")
    elif label_totals["buyer"] and label_totals["merchant"]:
        role_evidence = "exact_role_labels_on_sampled_rows"
    elif label_totals["replied"] or label_totals["unreplied"]:
        role_evidence = "reply_labels_without_direction"
        human.append("只看到已回复或未回复，还不能区分买家和商家。")
    else:
        role_evidence = "absent"
    return {
        "groups": groups,
        "shell_ids": shell,
        "hidden_row_count": hidden,
        "virtual_list": virtual,
        "id_evidence": id_evidence,
        "id_stability": "unproven_single_snapshot",
        "can_use_for_dedupe": False,
        "time_evidence": time_evidence,
        "can_treat_time_as_message_time": False,
        "role_evidence": role_evidence,
        "can_treat_labels_as_reply_status": False,
        "human_checks": human,
        "values_included": False,
        "production_mapping_validated": False,
    }


def _error(reason: str) -> dict[str, Any]:
    return {
        "status": "diagnostic_error",
        "reason": reason,
        "nodes": None,
        "ignored_unstable_attributes": [],
        "selector_candidates": [],
        "selectors_inferred": False,
        "platform_unsupported": False,
        "text_included": False,
        "values_included": False,
        "real_store_connected": False,
        "production_mapping_validated": False,
        "collection_enabled": False,
    }
