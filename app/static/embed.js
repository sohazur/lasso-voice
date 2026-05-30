/* Lasso Voice — embeddable cart-recovery snippet.
 *
 * Drop on any store:
 *   <script src="https://YOUR_HOST/embed.js" data-store="acme"
 *           data-store-name="Acme Coffee"></script>
 *
 * The store sets window.LASSO_CART before abandonment, e.g.:
 *   window.LASSO_CART = { currency:"USD", total: 120,
 *     items:[{title:"Beans", qty:1, price:120}] };
 *
 * On exit-intent (mouse leaves the top of the viewport), Lasso shows a CONSENTED
 * "Call me to finish" modal. On submit it POSTs the cart + phone to /consent on the
 * same origin the script was served from, which places the recovery call.
 */
(function () {
  var script = document.currentScript;
  var STORE_ID = (script && script.getAttribute("data-store")) || "acme";
  var STORE_NAME = (script && script.getAttribute("data-store-name")) || "the store";
  // Call /consent on the host that served this script (where the FastAPI app runs).
  var BASE = (script && script.src) ? new URL(script.src).origin : window.location.origin;

  var shown = false;
  var armed = false;
  // Arm exit-intent only once the cart is non-empty (something to recover).
  function cart() { return window.LASSO_CART || null; }
  function hasCart() { var c = cart(); return c && c.total > 0; }

  // ── modal ────────────────────────────────────────────────────────────────
  function injectStyles() {
    if (document.getElementById("lasso-style")) return;
    var s = document.createElement("style");
    s.id = "lasso-style";
    s.textContent =
      ".lasso-ov{position:fixed;inset:0;background:rgba(8,10,13,.6);backdrop-filter:blur(3px);" +
      "display:flex;align-items:center;justify-content:center;z-index:2147483647;" +
      "font:15px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Inter,sans-serif}" +
      ".lasso-card{background:#fff;color:#14181d;max-width:380px;width:calc(100% - 40px);" +
      "border-radius:18px;padding:26px 24px;box-shadow:0 24px 60px rgba(0,0,0,.35)}" +
      ".lasso-card h3{margin:0 0 6px;font-size:20px;letter-spacing:-.02em}" +
      ".lasso-card p{margin:0 0 16px;color:#5b6671;font-size:14px}" +
      ".lasso-sum{background:#f4f6f8;border-radius:10px;padding:10px 12px;margin:0 0 16px;" +
      "font-size:13px;color:#3a444d}" +
      ".lasso-card input{width:100%;box-sizing:border-box;padding:12px 14px;border:1px solid #d6dde3;" +
      "border-radius:10px;font:inherit;margin-bottom:12px}" +
      ".lasso-btn{width:100%;border:0;border-radius:10px;padding:13px;font:inherit;font-weight:600;" +
      "cursor:pointer;color:#fff;background:#2563eb}.lasso-btn:disabled{opacity:.5}" +
      ".lasso-x{position:absolute;top:14px;right:16px;cursor:pointer;color:#9aa6b1;font-size:20px;border:0;background:0}" +
      ".lasso-fine{font-size:11px;color:#9aa6b1;margin:10px 0 0;text-align:center}" +
      ".lasso-ok{text-align:center;padding:8px 0}";
    document.head.appendChild(s);
  }

  function summaryText() {
    var c = cart(); if (!c || !c.items) return "";
    var names = c.items.slice(0, 3).map(function (i) {
      return (i.qty > 1 ? i.qty + "× " : "") + i.title;
    });
    var sym = { USD: "$", EUR: "€", GBP: "£" }[(c.currency || "USD").toUpperCase()] || "";
    return names.join(", ") + " · " + sym + Number(c.total).toFixed(2);
  }

  function close() {
    var ov = document.getElementById("lasso-ov");
    if (ov) ov.remove();
  }

  function open() {
    if (shown || !hasCart()) return;
    shown = true;
    injectStyles();
    var ov = document.createElement("div");
    ov.className = "lasso-ov"; ov.id = "lasso-ov";
    ov.innerHTML =
      '<div class="lasso-card" style="position:relative">' +
      '<button class="lasso-x" aria-label="close">×</button>' +
      "<h3>Want a hand finishing up?</h3>" +
      "<p>Leave your number and " + STORE_NAME +
      " will call you in a few seconds to help complete your order.</p>" +
      '<div class="lasso-sum">' + summaryText() + "</div>" +
      '<input id="lasso-phone" type="tel" placeholder="+1 555 123 4567" autocomplete="tel" />' +
      '<button class="lasso-btn" id="lasso-go">Call me to finish</button>' +
      '<p class="lasso-fine">By tapping, you consent to a one-time automated call about your cart.</p>' +
      "</div>";
    document.body.appendChild(ov);

    ov.querySelector(".lasso-x").onclick = close;
    ov.addEventListener("click", function (e) { if (e.target === ov) close(); });
    ov.querySelector("#lasso-go").onclick = submit;
    ov.querySelector("#lasso-phone").focus();
  }

  function normPhone(raw) {
    var digits = (raw || "").replace(/[^\d+]/g, "");
    if (digits && digits[0] !== "+") {
      // naive US default if they typed 10 digits without a country code
      if (digits.length === 10) digits = "+1" + digits;
      else digits = "+" + digits;
    }
    return digits;
  }

  function submit() {
    var c = cart();
    var phone = normPhone(document.getElementById("lasso-phone").value);
    var btn = document.getElementById("lasso-go");
    if (!(phone.startsWith("+") && phone.length >= 9)) {
      document.getElementById("lasso-phone").style.borderColor = "#ff5a5f";
      return;
    }
    btn.disabled = true; btn.textContent = "Calling you…";
    fetch(BASE + "/consent", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        store_id: STORE_ID, store_name: STORE_NAME,
        phone: phone, consent: true,
        currency: c.currency || "USD", total: c.total || 0,
        items: (c.items || []).map(function (i) {
          return { title: i.title, qty: i.qty || 1, price: i.price || 0 };
        })
      })
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        var card = document.querySelector(".lasso-card");
        if (res.ok) {
          card.innerHTML = '<div class="lasso-ok"><h3>📞 Calling you now</h3>' +
            "<p>Pick up — " + STORE_NAME + " is on the line to finish your cart.</p></div>";
          setTimeout(close, 4000);
        } else {
          btn.disabled = false; btn.textContent = "Try again";
          var msg = (res.j && (res.j.message || res.j.error)) || "Could not place the call.";
          card.querySelector("p").textContent = msg;
        }
      })
      .catch(function () { btn.disabled = false; btn.textContent = "Try again"; });
  }

  // ── exit-intent ────────────────────────────────────────────────────────────
  function arm() {
    if (armed) return; armed = true;
    document.addEventListener("mouseout", function (e) {
      if (!e.relatedTarget && e.clientY <= 0) open(); // cursor left via the top
    });
    // mobile / fallback: trigger after a dwell with an abandoned cart
    setTimeout(function () { if (hasCart()) open(); }, 25000);
  }

  // Expose a manual trigger so a store's "checkout" page can force it on abandon.
  window.Lasso = { open: open, arm: arm };
  if (document.readyState !== "loading") arm();
  else document.addEventListener("DOMContentLoaded", arm);
})();
