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

  // Azure spend chart: a small tooltip for each day (the table under the chart has the same numbers).
  var tip = document.getElementById("chart-tip");
  if (tip) {
    document.addEventListener("mousemove", function (event) {
      var hit = event.target.closest && event.target.closest(".cost-chart [data-tip]");
      if (!hit) { tip.hidden = true; return; }
      tip.textContent = hit.getAttribute("data-tip");
      tip.hidden = false;
      var x = Math.min(event.clientX + 12, window.innerWidth - tip.offsetWidth - 8);
      tip.style.left = Math.max(8, x) + "px";
      tip.style.top = (event.clientY - tip.offsetHeight - 10) + "px";
    });
  }

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

  // Blueprint: a numbered mark (or "See the fix") opens that finding; notes and rooms light up together.
  function openFinding(id) {
    var target = id && document.getElementById(id);
    if (target && target.tagName === "DETAILS") target.open = true;
  }
  document.addEventListener("click", function (event) {
    var link = event.target.closest(".blueprint a[href^='#']");
    if (link) openFinding(decodeURIComponent(link.getAttribute("href").slice(1)));
  });
  openFinding(decodeURIComponent(location.hash.slice(1)));
  window.addEventListener("hashchange", function () { openFinding(decodeURIComponent(location.hash.slice(1))); });

  var plan = document.querySelector(".blueprint");
  if (plan) {
    var lit = [];
    var light = function (room, note) {
      lit.forEach(function (el) { el.classList.remove("hl"); });
      lit = [];
      if (!room) return;
      plan.querySelectorAll(".bp-room[data-room='" + room + "']").forEach(function (el) { lit.push(el); });
      plan.querySelectorAll(note ? "[data-note='" + note + "']" : ".bp-notes li[data-room='" + room + "'], .bp-marker[data-room='" + room + "']")
        .forEach(function (el) { lit.push(el); });
      lit.forEach(function (el) { el.classList.add("hl"); });
    };
    var over = function (event) {
      var el = event.target.closest("[data-note], [data-room]");
      if (!el || !plan.contains(el)) return light(null);
      light(el.getAttribute("data-room"), el.getAttribute("data-note"));
    };
    plan.addEventListener("mouseover", over);
    plan.addEventListener("focusin", over);
    plan.addEventListener("mouseleave", function () { light(null); });
  }

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
