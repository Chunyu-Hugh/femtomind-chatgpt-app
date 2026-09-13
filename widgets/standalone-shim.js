/* FemtoMind standalone widget shim.
 * Lets the ChatGPT-bridge widgets (composer / ops panel) run as plain web pages:
 * callTool -> JSON-RPC over the app's own /mcp endpoint (Bearer token), with the
 * tool output and widget state carried through localStorage + postMessage the
 * same way the plugin host does. Any agent (Codex, Claude Code, any LLM) can
 * open /app/composer or /app/panel and the UI just works.
 */
(function () {
  var WIDGET = window.__FM_WIDGET || "composer";
  var token = (new URLSearchParams(location.search).get("token") || "") ||
              (localStorage.getItem("femtomind_token") || "");

  function callTool(name, args) {
    if (!token) { showToken(false); return Promise.reject(new Error("authentication required")); }
    return fetch("/mcp", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": "Bearer " + token,
      },
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: Math.floor(Math.random() * 1e9),
        method: "tools/call",
        params: { name: name, arguments: args || {} },
      }),
    }).then(function (r) {
      if (r.status === 401) { showToken(true); throw new Error("authentication required"); }
      return r.json().catch(function () { throw new Error("bad response"); });
    }).then(function (d) {
      if (d && d.error) throw new Error(d.error.message || "tool error");
      return (d && d.result) || d;   // widgets unwrap structuredContent/content themselves
    });
  }

  function setWidgetState(st) {
    try { localStorage.setItem("femtomind_widget_" + WIDGET, JSON.stringify(st)); } catch (e) {}
  }
  function getWidgetState() {
    try { return JSON.parse(localStorage.getItem("femtomind_widget_" + WIDGET) || "null"); } catch (e) { return null; }
  }
  function sendFollowUpMessage(m) { console.log("[femtomind] follow-up:", m && m.prompt); }

  function bootWidget() {
    var kind = WIDGET === "panel" ? "panel" : "composer";
    return fetch("/app/boot/" + kind, { headers: { "Authorization": "Bearer " + token } })
      .then(function (r) {
        if (r.status === 401) { showToken(true); throw new Error("authentication required"); }
        return r.json();
      })
      .then(function (b) {
        window.openai.toolOutput = b;
        hideToken();
        window.postMessage({ type: "openai:set_globals" }, "*");
      });
  }

  // ---- token overlay ------------------------------------------------------
  var ov = null;
  function showToken(wrong) {
    if (ov) { ov.style.display = "flex"; if (wrong) ov.querySelector(".err").textContent = "Invalid or expired token."; return; }
    ov = document.createElement("div");
    ov.innerHTML =
      '<div style="position:fixed;inset:0;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;z-index:9999">' +
      '<div style="background:#fff;color:#111;border-radius:12px;padding:18px 20px;max-width:420px;font:14px system-ui,sans-serif;box-shadow:0 10px 40px rgba(0,0,0,.3)">' +
      '<div style="font-weight:700;margin-bottom:6px">FemtoMind sign-in</div>' +
      '<div style="color:#555;font-size:12px;margin-bottom:10px">Paste your FemtoMind access token to use this UI.' +
      ' Claude Code / other agents: open this URL and the token unlocks the same tools the agent uses.</div>' +
      '<input type="password" placeholder="access token" style="width:100%;padding:8px 10px;border:1px solid #ccc;border-radius:8px;margin-bottom:8px">' +
      '<div class="err" style="color:#b45309;font-size:12px;margin-bottom:6px"></div>' +
      '<button style="width:100%;padding:9px;border:none;border-radius:8px;background:#0c7a40;color:#fff;font-weight:600;cursor:pointer">Continue</button>' +
      '</div></div>';
    document.body.appendChild(ov);
    var inp = ov.querySelector("input"), btn = ov.querySelector("button");
    btn.onclick = function () {
      token = inp.value.trim();
      if (!token) return;
      localStorage.setItem("femtomind_token", token);
      bootWidget().catch(function () {});
    };
    inp.onkeydown = function (e) { if (e.key === "Enter") btn.onclick(); };
  }
  function hideToken() { if (ov) ov.style.display = "none"; }

  window.openai = {
    callTool: callTool,
    setWidgetState: setWidgetState,
    widgetState: getWidgetState(),
    sendFollowUpMessage: sendFollowUpMessage,
    toolOutput: null,
  };

  // The boot endpoints are public shells (no data): always load the UI first,
  // and only demand a token when a tool call actually needs one.
  bootWidget().catch(function () {
    showToken(false);
  });
})();
