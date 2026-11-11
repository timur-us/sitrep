// The advisor page (M1 PR 11): one question to POST /api/ask, one answer with its
// sources and grounding counts.
//
// Everything the response carries is untrusted: the answer is model output, and a
// source's title can come from an attacker's e-mail (threat model, path 1). It reaches
// the page through textContent only. Nothing is parsed as HTML or Markdown, the only
// elements created are list items and spans, and no link, image, attribute or request
// is ever built from it. tests/test_ui_page.py scans this file for the sinks that would
// break that rule; tests/web/test_answer.py renders hostile output in Chromium.
//
// The grounding counts say where a figure was found, never that it is true (PR 9).
"use strict";

(() => {
  const ASK = "/api/ask";
  const MAX_QUESTION = 2000;
  const TRACE_ID = /^[0-9a-f]{32}$/;
  const DECIMAL = /^[0-9]{1,12}(?:\.[0-9]{1,12})?$/;
  const STOPS = Object.freeze({
    end_turn: "",
    refusal: "The model declined to answer.",
    budget: "Stopped early: the token budget for this question ran out.",
    max_turns: "Stopped early: the turn limit for this question was reached.",
  });
  const KINDS = Object.freeze({ document: "document", product_sheet: "product sheet" });
  const FAILURES = Object.freeze({
    401: "Not signed in.",
    403: "The request was refused.",
    422: "The question was not accepted: it needs 1 to 2000 characters.",
    429: "Too many questions in a short time. Wait a moment, then ask again.",
    503: "The service is unavailable.",
  });

  const byId = (id) => document.getElementById(id);
  let latest = 0;

  function isCount(value) {
    return Number.isSafeInteger(value) && value >= 0;
  }

  function cost(value) {
    if (typeof value === "number" && Number.isFinite(value) && value >= 0) {
      return value;
    }
    if (typeof value === "string" && DECIMAL.test(value)) {
      return Number(value);
    }
    return null;
  }

  function isCitation(item) {
    return (
      item !== null &&
      typeof item === "object" &&
      typeof item.record_id === "string" &&
      typeof item.title === "string" &&
      typeof item.kind === "string"
    );
  }

  // The plan's contract ("The API contract"); anything else renders nothing.
  function isAnswer(data) {
    if (data === null || typeof data !== "object") {
      return false;
    }
    const grounding = data.grounding;
    const usage = data.usage;
    return (
      typeof data.answer === "string" &&
      Array.isArray(data.citations) &&
      data.citations.every(isCitation) &&
      grounding !== null &&
      typeof grounding === "object" &&
      isCount(grounding.figures) &&
      isCount(grounding.unmatched) &&
      isCount(grounding.unknown_citations) &&
      grounding.unmatched <= grounding.figures &&
      usage !== null &&
      typeof usage === "object" &&
      isCount(usage.input_tokens) &&
      isCount(usage.output_tokens) &&
      cost(data.cost_usd) !== null &&
      typeof data.stop === "string" &&
      Object.hasOwn(STOPS, data.stop) &&
      typeof data.trace_id === "string" &&
      TRACE_ID.test(data.trace_id)
    );
  }

  function figures(grounding) {
    const total = grounding.figures;
    if (total === 0) {
      return "The answer has no figures.";
    }
    const found = total - grounding.unmatched;
    return `${found} of ${total} ${total === 1 ? "figure" : "figures"} found in the sources.`;
  }

  function unknown(grounding) {
    const count = grounding.unknown_citations;
    if (count === 0) {
      return "";
    }
    return count === 1
      ? "1 cited id is not among the sources."
      : `${count} cited ids are not among the sources.`;
  }

  function span(className, value) {
    const element = document.createElement("span");
    element.className = className;
    element.textContent = value;
    return element;
  }

  function citation(item) {
    const entry = document.createElement("li");
    entry.append(
      span("title", item.title),
      span("kind", Object.hasOwn(KINDS, item.kind) ? KINDS[item.kind] : item.kind),
      span("record", item.record_id),
    );
    return entry;
  }

  function clear() {
    byId("result").hidden = true;
    byId("status").textContent = "";
    byId("answer").textContent = "";
    byId("stop").textContent = "";
    byId("stop").hidden = true;
    byId("citations").replaceChildren();
    byId("figures").textContent = "";
    byId("unknown").textContent = "";
    byId("trace").textContent = "";
    byId("cost").textContent = "";
    byId("tokens").textContent = "";
  }

  function fail(message, response) {
    const trace = response ? response.headers.get("x-trace-id") : null;
    const suffix = trace !== null && TRACE_ID.test(trace) ? ` Trace id: ${trace}` : "";
    byId("status").textContent = message + suffix;
  }

  function show(data) {
    byId("answer").textContent = data.answer;
    const reason = STOPS[data.stop];
    byId("stop").textContent = reason;
    byId("stop").hidden = reason === "";
    byId("citations").replaceChildren(...data.citations.map(citation));
    byId("figures").textContent = figures(data.grounding);
    byId("unknown").textContent = unknown(data.grounding);
    byId("trace").textContent = data.trace_id;
    byId("cost").textContent = `USD ${cost(data.cost_usd).toFixed(4)}`;
    byId("tokens").textContent =
      `${data.usage.input_tokens} in, ${data.usage.output_tokens} out`;
    byId("result").hidden = false;
  }

  async function submit(event) {
    event.preventDefault();
    const question = byId("question").value.trim();
    // A new submit supersedes any request still pending: its response is dropped.
    const mine = ++latest;
    const button = byId("ask").querySelector("button");
    clear();
    if (question.length === 0 || question.length > MAX_QUESTION) {
      byId("status").textContent =
        question.length === 0
          ? "Ask a question first."
          : `The question is too long: at most ${MAX_QUESTION} characters.`;
      button.disabled = false;
      button.textContent = "Ask";
      return;
    }
    button.disabled = true;
    button.textContent = "Asking...";
    let response = null;
    try {
      // The default mode: Chromium sends this same-origin POST with the page's real
      // Origin, which the Origin check of every non-GET route requires, even under the
      // app's Referrer-Policy: no-referrer, where a form sends "null". Measured in
      // tests/web/test_answer.py (in same-origin mode too, M1 PR 11's mutant run). No
      // redirect is followed.
      response = await fetch(ASK, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ question }),
        credentials: "same-origin",
        cache: "no-store",
        redirect: "error",
      });
      if (mine !== latest) {
        return;
      }
      if (!response.ok) {
        const status = response.status;
        const known = Object.hasOwn(FAILURES, status);
        fail(known ? FAILURES[status] : `The request failed (HTTP ${status}).`, response);
        return;
      }
      let data = null;
      try {
        data = await response.json();
      } catch {
        data = null;
      }
      if (mine !== latest) {
        return;
      }
      if (!isAnswer(data)) {
        fail("Unexpected response.", response);
        return;
      }
      byId("status").textContent = "";
      show(data);
    } catch {
      if (mine === latest) {
        fail("The request failed: no answer from the server.", response);
      }
    } finally {
      if (mine === latest) {
        button.disabled = false;
        button.textContent = "Ask";
      }
    }
  }

  function start() {
    byId("ask").addEventListener("submit", submit);
    // Ctrl+Enter or Cmd+Enter in the box asks, as the button does.
    byId("question").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        byId("ask").requestSubmit();
      }
    });
  }

  start();
})();
