/*
 * Panel behaviour that used to live in inline event-handler attributes.
 *
 * The CSP (app/admin/web/headers.py) is script-src 'self' 'nonce-…' with no
 * 'unsafe-inline' and no 'unsafe-hashes', so the browser never runs an
 * onsubmit="…" attribute. Destructive forms were posting with no confirmation
 * at all. Loaded from /static, which 'self' admits; one delegated listener
 * per behaviour, so templates only declare data-* attributes.
 */
(function () {
  "use strict";

  // <form data-confirm="Question?"> — ask before a destructive POST.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    var question = form.getAttribute("data-confirm");
    if (question !== null && !window.confirm(question)) {
      event.preventDefault();
      return;
    }
    if (question !== null) {
      var confirmed = form.elements.namedItem("confirmed");
      if (confirmed) confirmed.value = "1";
    }
  });

  // <tr data-href="/users/…"> — the whole row navigates. A click that lands on
  // a real control inside the row keeps that control's own behaviour.
  document.addEventListener("click", function (event) {
    var row = event.target.closest("[data-href]");
    if (!row || event.target.closest("a, button, input, select, textarea, label")) return;
    var href = row.getAttribute("data-href");
    if (event.ctrlKey || event.metaKey) {
      window.open(href, "_blank");
    } else {
      window.location.href = href;
    }
  });

  // <select data-autosubmit> — apply a filter as soon as it changes.
  document.addEventListener("change", function (event) {
    var el = event.target;
    if (el instanceof HTMLSelectElement && el.hasAttribute("data-autosubmit") && el.form) {
      el.form.submit();
    }
  });
})();
