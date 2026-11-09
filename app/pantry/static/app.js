// Pantry: tiny progressive enhancement. Every action is a same-origin JSON POST with X-Pantry: 1.
(() => {
  const $ = (s, el = document) => el.querySelector(s);
  const toast = (msg) => {
    const t = $("#toast"); if (!t) return;
    t.textContent = msg; t.classList.add("show");
    clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove("show"), 2600);
  };
  async function post(url, data) {
    let r;
    try {
      r = await fetch(url, { method: "POST", credentials: "same-origin", redirect: "manual",
        headers: { "Content-Type": "application/json", "X-Pantry": "1" }, body: JSON.stringify(data || {}) });
    } catch (e) { toast("Offline — try again when you have signal."); throw e; }
    if (r.type === "opaqueredirect" || r.status === 0) { location.reload(); throw new Error("signin"); }
    if (r.status === 401) { location.href = "/who?next=" + encodeURIComponent(location.pathname + location.search); throw new Error("who"); }
    if (!r.ok) { let m = "Something went wrong.";
      try { const j = await r.json(); m = j.detail || j.error || m; } catch (_) {}
      toast(m); throw new Error(m); }
    return r.json();
  }

  document.addEventListener("click", async (e) => {
    const b = e.target.closest("button"); if (!b) return;

    if (b.dataset.pick) {
      const on = b.getAttribute("aria-pressed") !== "true";
      const card = b.closest(".dish");
      b.setAttribute("aria-pressed", on); card.classList.toggle("picked", on);
      try {
        const r = await post(`/api/proposals/${b.dataset.pick}/pick`, { picked: on });
        $(".by", card).textContent = r.picked && r.by ? `by ${r.by}` : "";
        const c = $("#pickcount"); if (c) c.textContent = r.count;
        if (on && r.count === 5) toast("That's 5. Claude will build the list.");
        else if (r.count > 5) toast(`${r.count} picked — that's more than 5, which is fine too.`);
      } catch (_) { b.setAttribute("aria-pressed", !on); card.classList.toggle("picked", !on); }
    }

    else if (b.dataset.cooked) {
      const on = b.getAttribute("aria-pressed") !== "true";
      const li = b.closest("li");
      b.setAttribute("aria-pressed", on); li.classList.toggle("made", on);
      try {
        const r = await post(`/api/proposals/${b.dataset.cooked}/cooked`, { cooked: on });
        const c = $("#madecount"); if (c) c.textContent = r.count;
      } catch (_) { b.setAttribute("aria-pressed", !on); li.classList.toggle("made", !on); }
    }

    else if (b.dataset.decide) {
      const li = b.closest("[data-item]");
      const pressed = b.getAttribute("aria-pressed") === "true" && !li.classList.contains("guess");
      const d = pressed ? "" : b.dataset.decide;
      try {
        const r = await post(`/api/items/${li.dataset.item}/decide`, { decision: d });
        li.classList.remove("is-need", "is-have");
        if (r.effective) li.classList.add("is-" + r.effective);
        li.classList.toggle("guess", !r.decision && !!r.effective);
        li.querySelectorAll("[data-decide]").forEach(x =>
          x.setAttribute("aria-pressed", x.dataset.decide === r.effective));
      } catch (_) {}
    }

    else if (b.dataset.delItem) {
      try { await post(`/api/items/${b.dataset.delItem}/delete`); b.closest("li").remove(); } catch (_) {}
    }

    else if (b.dataset.delRequest) {
      try { await post(`/api/requests/${b.dataset.delRequest}/delete`); b.closest("li").remove(); } catch (_) {}
    }

    else if (b.hasAttribute("data-voice-have")) {
      if (!confirm("Remove it from the Mealie Grocery list?")) return;
      try { await post(`/api/voice/${b.dataset.voiceHave}/have`); b.closest("li").remove(); toast("Removed from Mealie."); } catch (_) {}
    }

    else if (b.dataset.receiptConfirm || b.dataset.receiptDiscard) {
      const discard = !!b.dataset.receiptDiscard;
      b.disabled = true;
      try {
        const r = await post("/api/receipts/confirm", { token: b.dataset.receiptConfirm || b.dataset.receiptDiscard, discard });
        toast(discard ? "Cancelled." : `Added ${r.saved}. Claude will read them at /receipt.`);
        setTimeout(() => location.href = "/receipts", 1000);
      } catch (_) { b.disabled = false; }
    }

    else if (b.hasAttribute("data-plan-week")) {
      const day = ($("#planday") || {}).value;
      b.disabled = true;
      try { await post("/api/jobs/plan-week", { week: day, note: ($("#plannote") || {}).value || "" });
        toast("Asked Claude. Ideas usually show up within 10 minutes.");
        setTimeout(() => location.reload(), 1200);
      } catch (_) { b.disabled = false; }
    }

    else if (b.hasAttribute("data-send")) {
      b.disabled = true;
      try {
        const r = await post("/api/send-to-mealie");
        toast(`Sent ${r.sent} to Mealie${r.already_there ? `, ${r.already_there} already there` : ""}.`);
        setTimeout(() => location.reload(), 1200);
      } catch (_) { b.disabled = false; }
    }

    else if (b.hasAttribute("data-idea")) {
      b.disabled = true;
      try { await post("/api/requests", { slug: b.dataset.slug, name: b.dataset.name, text: "" });
        toast("Suggested. Claude will consider it."); b.closest("li").remove();
      } catch (_) { b.disabled = false; }
    }

    else if (b.hasAttribute("data-save-note")) {
      try { await post("/api/week/note", { note: $("#weeknote").value }); toast("Saved."); } catch (_) {}
    }

    else if (b.dataset.r) {
      const li = b.closest("[data-rate]");
      const on = b.getAttribute("aria-pressed") !== "true";
      const rating = on ? Number(b.dataset.r) : 0;
      try {
        await post("/api/ratings", { week_id: li.dataset.week, slug: li.dataset.slug, rating, note: $(".rnote", li).value });
        li.querySelectorAll("[data-r]").forEach(x => x.setAttribute("aria-pressed", on && x === b));
      } catch (_) {}
    }
  });

  // rating notes save when you leave the field
  document.addEventListener("change", async (e) => {
    const inp = e.target.closest(".rnote"); if (!inp) return;
    const li = inp.closest("[data-rate]");
    const on = li.querySelector('[data-r][aria-pressed="true"]');
    try { await post("/api/ratings", { week_id: li.dataset.week, slug: li.dataset.slug,
      rating: on ? Number(on.dataset.r) : 0, note: inp.value }); toast("Saved."); } catch (_) {}
  });

  document.addEventListener("submit", async (e) => {
    const f = e.target;
    if (f.hasAttribute("data-request")) {
      e.preventDefault();
      const data = Object.fromEntries(new FormData(f));
      try { await post("/api/requests", data); toast("Sent to Claude.");
        setTimeout(() => location.href = f.dataset.after || location.pathname, 700); } catch (_) {}
    } else if (f.hasAttribute("data-add-item")) {
      e.preventDefault();
      const name = f.name.value.trim(); if (!name) return;
      try { await post("/api/items", { name }); location.reload(); } catch (_) {}
    }
  });

  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  // Receipt photos: multipart POST straight to the inbox (home network only; the server checks).
  document.addEventListener("change", async (e) => {
    const inp = e.target.closest("[data-receipt-upload] input[type=file]");
    if (!inp || !inp.files.length) return;
    const fd = new FormData();
    for (const f of inp.files) fd.append("photos", f);
    const label = inp.closest("label"); label.classList.add("busy");
    toast(`Sending ${inp.files.length}…`);
    try {
      const r = await fetch("/api/receipts", { method: "POST", credentials: "same-origin", redirect: "manual",
        headers: { "X-Pantry": "1" }, body: fd });
      if (r.status === 401) { location.href = "/who?next=/receipts"; return; }
      const j = await r.json().catch(() => ({}));
      if (!r.ok) { toast(j.detail || j.error || "Upload failed."); return; }
      toast(`Sent ${j.saved}. Claude will read them at /receipt.`);
      const w = $("#waiting"); if (w) w.textContent = `${j.waiting} photo${j.waiting === 1 ? "" : "s"} waiting for Claude.`;
    } catch (_) { toast("Couldn't reach Pantry. Are you on the home Wi-Fi?"); }
    finally { inp.value = ""; label.classList.remove("busy"); }
  });
  // While Claude is planning, refresh the home page every 30 s until the job finishes.
  if (document.querySelector("[data-job-active]")) setTimeout(() => location.reload(), 30000);
})();
