"use strict";
// Passkeys (WebAuthn) for the login page and the Household tab. The browser only offers them on https or localhost.
const Passkey = (() => {
  const toBuf = (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4)), (c) => c.charCodeAt(0)).buffer;
  const toB64 = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const usable = () => !!(window.PublicKeyCredential && window.isSecureContext && navigator.credentials);

  async function create(opts) {
    const pk = { ...opts, challenge: toBuf(opts.challenge), user: { ...opts.user, id: toBuf(opts.user.id) },
      excludeCredentials: (opts.excludeCredentials || []).map((c) => ({ ...c, id: toBuf(c.id) })) };
    const cred = await navigator.credentials.create({ publicKey: pk });
    return { id: cred.id, clientDataJSON: toB64(cred.response.clientDataJSON), attestationObject: toB64(cred.response.attestationObject),
      transports: cred.response.getTransports ? cred.response.getTransports() : [] };
  }

  // extra: { mediation: "conditional", signal } for the passkey offered in the username field's suggestions
  async function get(opts, extra = {}) {
    const pk = { ...opts, challenge: toBuf(opts.challenge), allowCredentials: (opts.allowCredentials || []).map((c) => ({ ...c, id: toBuf(c.id) })) };
    const cred = await navigator.credentials.get({ publicKey: pk, ...extra });
    return { id: cred.id, clientDataJSON: toB64(cred.response.clientDataJSON), authenticatorData: toB64(cred.response.authenticatorData),
      signature: toB64(cred.response.signature), userHandle: cred.response.userHandle ? toB64(cred.response.userHandle) : "" };
  }

  // a person cancelling the phone's prompt isn't an error worth a red message
  const cancelled = (e) => e && (e.name === "NotAllowedError" || e.name === "AbortError");
  // this device has logged in with (or saved) a passkey before: the login page asks for it straight away
  const KEY = "bunkr-passkey";
  const remembered = () => { try { return localStorage.getItem(KEY) === "1"; } catch { return false; } };
  const remember = (on) => { try { on ? localStorage.setItem(KEY, "1") : localStorage.removeItem(KEY); } catch {} };
  const autofill = async () => { try { return !!(await PublicKeyCredential.isConditionalMediationAvailable?.()); } catch { return false; } };
  return { usable, create, get, cancelled, remembered, remember, autofill };
})();
