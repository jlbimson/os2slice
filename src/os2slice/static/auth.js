// os2slice per-user Onshape sign-in (D-23). Three roles, picked by what the page has:
// - the sign-in panel: opens Onshape's sign-in in a pop-up (it can't be framed), then
//   claims the session the pop-up made, so the panel's own (partitioned) cookie is set;
// - the pop-up / tab Onshape sends back to: hands its one-time claim code to the panel
//   that opened it (same origin only), then closes; a plain tab goes on to its page;
// - the panel's "Sign out" link.
"use strict";
(() => {
  const done = document.getElementById("signed-in");
  if (done) {
    if (window.opener) {
      window.opener.postMessage(
        { type: "os2slice-signed-in", claim: done.dataset.claim }, window.location.origin);
      setTimeout(() => window.close(), 400);
    } else if (done.dataset.next) {
      window.location.replace(done.dataset.next);
    }
    return;
  }

  const post = (path, data) => fetch(path, {
    method: "POST", credentials: "same-origin", body: new URLSearchParams(data || {}),
  });

  const signout = document.getElementById("signout-button");
  if (signout) {
    signout.addEventListener("click", async (ev) => {
      ev.preventDefault();
      await post("/auth/sign-out");
      window.location.reload();
    });
  }

  const button = document.getElementById("signin-button");
  const note = document.getElementById("signin-note");
  if (!button) return;
  let popup = null;
  button.addEventListener("click", () => {
    popup = window.open("/auth/start", "os2slice-signin", "popup,width=520,height=720");
    if (!popup) note.textContent = "The pop-up was blocked. Allow pop-ups for this site, then try again.";
    else note.textContent = "Finish signing in in the Onshape window…";
  });
  window.addEventListener("message", async (ev) => {
    if (ev.origin !== window.location.origin || !popup || ev.source !== popup) return;
    if (!ev.data || ev.data.type !== "os2slice-signed-in" || typeof ev.data.claim !== "string") return;
    const res = await post("/auth/claim", { claim: ev.data.claim });
    if (res.ok) window.location.reload();
    else note.textContent = "Signing in didn't finish. Try again.";
  });
})();
