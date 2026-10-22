(function () {
  "use strict";
  var URL_JSON = "data/results.json";

  function fmt(r) {
    if (typeof r.value === "number") {
      if (r.unit === "points" || r.unit === "documents") { return r.value.toLocaleString("en-US"); }
      return String(r.value);
    }
    return r.value === null || r.value === undefined ? "n/a" : String(r.value);
  }
  function el(tag, text, cls) {
    var e = document.createElement(tag);
    if (text !== undefined) { e.textContent = text; }
    if (cls) { e.className = cls; }
    return e;
  }
  function link(href, text) {
    var a = el("a", text); a.href = href; a.rel = "noopener"; return a;
  }

  function render(rows) {
    var byId = {};
    rows.forEach(function (r) { byId[r.id] = r; });

    document.querySelectorAll("[data-result]").forEach(function (node) {
      var r = byId[node.getAttribute("data-result")];
      if (!r) { return; }
      var num = node.querySelector(".num");
      var meta = node.querySelector(".meta");
      if (num) { num.textContent = fmt(r); }
      if (meta) {
        meta.textContent = r.date + " · commit " + r.commit + " · ";
        meta.appendChild(link(r.source_url, "source"));
      }
    });

    var tbody = document.getElementById("results-body");
    if (tbody) {
      tbody.textContent = "";
      rows.forEach(function (r) {
        var tr = el("tr");
        tr.appendChild(el("td", r.label));
        tr.appendChild(el("td", fmt(r) + (r.unit && r.unit !== "status" ? " " + r.unit : ""), "n"));
        tr.appendChild(el("td", r.date, "n"));
        var c = el("td"); c.appendChild(el("code", r.commit)); tr.appendChild(c);
        tr.appendChild(el("td", r.profile));
        var s = el("td"); s.appendChild(link(r.source_url, "record")); tr.appendChild(s);
        tr.appendChild(el("td", r.note || ""));
        tbody.appendChild(tr);
      });
    }
    document.querySelectorAll(".nojs").forEach(function (n) { n.hidden = true; });
  }

  if (document.querySelector("[data-result], #results-body")) {
    fetch(URL_JSON, { cache: "no-cache" })
      .then(function (res) { if (!res.ok) { throw new Error("http " + res.status); } return res.json(); })
      .then(render)
      .catch(function () {
        document.querySelectorAll(".nojs").forEach(function (n) { n.hidden = false; });
      });
  }

  var demo = document.getElementById("demo");
  if (demo) {
    var v = demo.querySelector("video");
    if (v) {
      v.addEventListener("loadedmetadata", function () { demo.hidden = false; });
    }
  }
}());
