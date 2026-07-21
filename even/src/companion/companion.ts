/**
 * Even Hub companion page.
 *
 * The canonical management UI is the server-hosted web app (mobile-responsive),
 * so this in-WebView page keeps only what must sit *beside the glasses*: the api
 * server setting + sign-in (which store the server URL and bearer token the lens
 * WS reads), plus a link that opens the server's web UI for everything else
 * (history, status, users).
 *
 * Like the web client, the api is a REQUIRED, user-editable setting — you point
 * Tenir at your own self-hosted instance and then sign in. Everything flows
 * through the shared `@tenir/client-core` REST client.
 */

import { ApiError, describeLoginError, login, logout, me, type Principal } from "@tenir/client-core";

import { applyServerUrl, config, isServerConfigured } from "../config";

const app = document.getElementById("app")!;
const toast = document.getElementById("toast")!;

function notify(message: string, kind: "ok" | "err" = "ok"): void {
  toast.textContent = message;
  toast.className = kind;
  toast.style.display = "block";
  window.setTimeout(() => (toast.style.display = "none"), 4000);
}

// --- tiny DOM helper --------------------------------------------------------

type Child = Node | string;
function h<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  props: Partial<HTMLElementTagNameMap[K]> & { onclick?: () => void } = {},
  ...children: Child[]
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  const { onclick, ...rest } = props;
  Object.assign(node, rest);
  if (onclick) node.addEventListener("click", onclick);
  for (const c of children) node.append(c);
  return node;
}

function section(title: string, ...children: Child[]): HTMLElement {
  return h("section", {}, h("h2", { textContent: title }), ...children);
}

function muted(text: string): HTMLElement {
  return h("p", { className: "muted", textContent: text });
}

// --- sections ---------------------------------------------------------------

/**
 * The api server setting — the first, required step. Points the shared REST client
 * (and, on its next launch, the lens) at the user's self-hosted instance.
 */
function renderServer(): HTMLElement {
  const input = h("input", { placeholder: "wss://your-server/ws", value: config.apiWsUrl });
  input.style.flexGrow = "1";
  const connect = h("button", {
    textContent: "Connect",
    onclick: () => {
      const applied = applyServerUrl(input.value);
      if (!applied) {
        notify("Enter a valid ws:// or wss:// server URL", "err");
        return;
      }
      notify(`Connected to ${applied.httpBaseUrl}`);
      void refresh();
    },
  });
  return section(
    "Server",
    muted("Point Tenir at your self-hosted api. Required before you can sign in."),
    h("div", { className: "row" }, input, connect),
  );
}

/** Sign-in form (auth is always required; the lens WS reuses the stored token). */
function renderLogin(): HTMLElement {
  const user = h("input", { placeholder: "username" });
  const pass = h("input", { placeholder: "password", type: "password" });
  return section(
    "Log in",
    h("div", { className: "row" }, user, pass),
    h("button", {
      textContent: "Log in",
      // A dedicated handler so login failures get the friendly
      // wrong-credentials / can't-reach-server messages.
      onclick: async () => {
        try {
          await login(user.value, pass.value);
          notify("Logged in");
          await refresh();
        } catch (err) {
          notify(describeLoginError(err), "err");
        }
      },
    }),
    muted("Session history, system status and user management live in the web app."),
  );
}

/** Logged-in account box + the doorway to the server-hosted web UI. */
function renderAccount(principal: Principal): HTMLElement {
  return section(
    "Account",
    h("div", { className: "row" }, `${principal.userId} · ${principal.household} · ${principal.role}`),
    h("div", { className: "row" },
      h("a", {
        textContent: "Open the Tenir web app →",
        href: config.apiHttpUrl,
        target: "_blank",
        rel: "noreferrer",
      }),
    ),
    muted("History, status and user management all live in the web app, served by your server."),
    h("button", {
      textContent: "Log out",
      onclick: () => {
        logout();
        void refresh();
      },
    }),
  );
}

// --- mount ------------------------------------------------------------------

async function refresh(): Promise<void> {
  const server = renderServer();

  // Step 1: a server URL is required before anything talks to the api.
  if (!isServerConfigured()) {
    app.replaceChildren(server, muted("Set your server above to continue."));
    return;
  }

  // Step 2: auth is always required — resolve the principal, else show sign-in.
  let principal: Principal | null = null;
  try {
    principal = await me();
  } catch (err) {
    if (err instanceof ApiError && err.status !== 401) {
      notify(`${err.status}: ${err.message}`, "err");
    }
    principal = null; // not logged in (or the server can't be reached)
  }
  if (!principal) {
    app.replaceChildren(server, renderLogin());
    return;
  }

  // Step 3: signed in — everything else happens in the server's web UI.
  app.replaceChildren(server, renderAccount(principal));
}

void refresh().catch((err) => notify(String(err), "err"));
