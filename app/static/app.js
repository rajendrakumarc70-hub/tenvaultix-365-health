// Small progressive enhancements. Every page works without JavaScript.
(function () {
  document.addEventListener("click", function (event) {
    var printer = event.target.closest("[data-print]");
    if (printer) {
      event.preventDefault();
      window.print();
      return;
    }
    var copier = event.target.closest("[data-copy]");
    if (copier && navigator.clipboard) {
      event.preventDefault();
      navigator.clipboard.writeText(copier.getAttribute("data-copy")).then(function () {
        var label = copier.textContent;
        copier.textContent = "Copied";
        setTimeout(function () { copier.textContent = label; }, 1800);
      });
      return;
    }
    var toggler = event.target.closest("[data-toggle-all]");
    if (toggler) {
      var target = document.getElementById(toggler.getAttribute("data-toggle-all"));
      if (!target) return;
      var items = target.querySelectorAll("details");
      var anyClosed = Array.prototype.some.call(items, function (d) { return !d.open; });
      items.forEach(function (d) { d.open = anyClosed; });
      toggler.textContent = anyClosed ? "Collapse all" : "Expand all";
    }
  });

  // Reports print with every finding expanded, then go back to how they were.
  window.addEventListener("beforeprint", function () {
    document.querySelectorAll("details").forEach(function (d) {
      d.setAttribute("data-was-open", d.open ? "1" : "0");
      d.open = true;
    });
  });
  window.addEventListener("afterprint", function () {
    document.querySelectorAll("details[data-was-open]").forEach(function (d) {
      d.open = d.getAttribute("data-was-open") === "1";
    });
  });

  // Typed confirmation for destructive actions.
  document.querySelectorAll("form[data-confirm-word]").forEach(function (form) {
    var input = form.querySelector("input[name=confirm]");
    var button = form.querySelector("button[type=submit]");
    var word = form.getAttribute("data-confirm-word");
    if (!input || !button) return;
    var sync = function () { button.disabled = input.value.trim().toUpperCase() !== word; };
    input.addEventListener("input", sync);
    sync();
  });
})();
