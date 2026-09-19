// Harvest an accessibility-shaped view of the current frame.
//
// We compute role + accessible name ourselves (rather than using Playwright's
// a11y snapshot) for two reasons: we need a stable handle back to the element
// so we can act on it, and we need legacy-app hints the a11y tree drops --
// the surrounding table cell text that is the *de facto* label in server
// rendered apps with no <label for>.
//
// Elements are tagged with data-cua-ref, which is cleared on every harvest so
// refs are never stale across observations.
(function (framePrefix) {
  var ROLE_BY_TAG = {
    A: "link", BUTTON: "button", SELECT: "combobox", TEXTAREA: "textbox",
    H1: "heading", H2: "heading", H3: "heading", H4: "heading",
    H5: "heading", H6: "heading", FORM: "form", IMG: "image"
  };

  function roleOf(el) {
    var explicit = el.getAttribute("role");
    if (explicit) return explicit.trim().toLowerCase();
    if (el.tagName === "INPUT") {
      var t = (el.getAttribute("type") || "text").toLowerCase();
      if (t === "submit" || t === "button" || t === "reset" || t === "image") return "button";
      if (t === "checkbox") return "checkbox";
      if (t === "radio") return "radio";
      if (t === "hidden") return null;
      return "textbox";
    }
    return ROLE_BY_TAG[el.tagName] || null;
  }

  function clean(s) {
    return (s || "").replace(/\s+/g, " ").trim().slice(0, 120);
  }

  function labelText(el) {
    if (el.id) {
      var byFor = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (byFor) return byFor.innerText;
    }
    var wrapper = el.closest ? el.closest("label") : null;
    return wrapper ? wrapper.innerText : "";
  }

  function nameOf(el, role) {
    var candidates = [
      el.getAttribute("aria-label"),
      labelText(el),
      el.getAttribute("placeholder"),
      el.getAttribute("title"),
      el.getAttribute("alt")
    ];
    if (role === "button" && el.tagName === "INPUT") candidates.unshift(el.value);
    if (role !== "textbox" && role !== "combobox") candidates.push(el.innerText);
    if (el.getAttribute("name")) candidates.push(el.getAttribute("name"));
    for (var i = 0; i < candidates.length; i++) {
      var c = clean(candidates[i]);
      if (c) return c;
    }
    return "";
  }

  // The nearest human-meaningful text: legacy apps label fields with the
  // previous table cell far more often than with a <label>.
  function anchorOf(el) {
    var cell = el.closest ? el.closest("td, th, li, fieldset, .form_group") : null;
    if (cell) {
      var prev = cell.previousElementSibling;
      if (prev && clean(prev.innerText)) return clean(prev.innerText);
      var legend = cell.querySelector ? cell.querySelector("legend") : null;
      if (legend && clean(legend.innerText)) return clean(legend.innerText);
    }
    var row = el.closest ? el.closest("tr, li, .inventory_item, .cart_item") : null;
    if (row && clean(row.innerText)) return clean(row.innerText);
    var parent = el.parentElement;
    return parent ? clean(parent.innerText) : "";
  }

  function cssPath(el) {
    var parts = [];
    var node = el;
    while (node && node.nodeType === 1 && parts.length < 6) {
      var part = node.tagName.toLowerCase();
      if (node.id && !/^\d/.test(node.id)) {
        parts.unshift(part + "#" + CSS.escape(node.id));
        break;
      }
      var parent = node.parentElement;
      if (parent) {
        var siblings = Array.prototype.filter.call(parent.children, function (c) {
          return c.tagName === node.tagName;
        });
        if (siblings.length > 1) part += ":nth-of-type(" + (siblings.indexOf(node) + 1) + ")";
      }
      parts.unshift(part);
      node = parent;
    }
    return parts.join(" > ");
  }

  function visible(el) {
    if (!el.getClientRects || el.getClientRects().length === 0) return false;
    var style = window.getComputedStyle(el);
    return style.visibility !== "hidden" && style.display !== "none" && style.opacity !== "0";
  }

  document.querySelectorAll("[data-cua-ref]").forEach(function (el) {
    el.removeAttribute("data-cua-ref");
  });

  var nodes = document.querySelectorAll(
    "a, button, input, select, textarea, [role], h1, h2, h3, h4, h5, h6"
  );
  var results = [];
  var counts = {};
  var index = 0;

  for (var i = 0; i < nodes.length && results.length < 250; i++) {
    var el = nodes[i];
    var role = roleOf(el);
    if (!role) continue;
    if (!visible(el)) continue;
    var name = nameOf(el, role);
    if (role === "heading" && !name) continue;

    var key = role + "|" + name;
    counts[key] = (counts[key] === undefined) ? 0 : counts[key] + 1;

    var ref = framePrefix + (index++);
    el.setAttribute("data-cua-ref", ref);

    results.push({
      ref: ref,
      role: role,
      name: name,
      value: (el.value !== undefined && role !== "button") ? clean(el.value) : null,
      enabled: !el.disabled && el.getAttribute("aria-disabled") !== "true",
      visible: true,
      ordinal: counts[key],
      anchor_text: anchorOf(el),
      css_hint: cssPath(el)
    });
  }

  return {
    elements: results,
    text: clean_multiline(document.body ? document.body.innerText : ""),
    title: document.title
  };

  function clean_multiline(s) {
    return (s || "")
      .split("\n")
      .map(function (line) { return line.trim(); })
      .filter(function (line) { return line.length > 0; })
      .slice(0, 120)
      .join("\n");
  }
})
