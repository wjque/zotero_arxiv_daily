"""Build a self-contained, responsive static recommendation site."""

# ruff: noqa: E501

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from zotero_arxiv_daily.core.errors import ConfigurationError
from zotero_arxiv_daily.security.encryption import encrypt_json
from zotero_arxiv_daily.site.models import PublishedRecommendationSet


@dataclass(frozen=True, slots=True)
class SiteBuildResult:
    output_directory: Path
    encrypted: bool
    recommendation_count: int


def build_site(
    recommendations: PublishedRecommendationSet,
    output_directory: Path,
    *,
    public_output: bool,
    passphrase: str | None,
    feedback_repository: str | None = None,
) -> SiteBuildResult:
    """Build a static site atomically, keeping recommendation data encrypted by default."""

    if public_output and passphrase:
        raise ConfigurationError("passphrase must be unset when public output is enabled")
    if not public_output and not passphrase:
        raise ConfigurationError("Pages passphrase is required for encrypted output")
    if feedback_repository and not _valid_repository(feedback_repository):
        raise ConfigurationError("feedback_repository must use the owner/repository format")
    payload = recommendations.to_dict()
    output_directory.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output_directory.name}.", dir=output_directory.parent))
    try:
        data_directory = stage / "data"
        assets_directory = stage / "assets"
        data_directory.mkdir()
        assets_directory.mkdir()
        encrypted = not public_output
        data_name = "recommendations.enc.json" if encrypted else "recommendations.json"
        data = _encrypt_payload(payload, passphrase) if encrypted else _json(payload)
        _write(data_directory / data_name, data)
        _write(
            data_directory / "site-config.json",
            _json(
                {
                    "schema_version": 1,
                    "encrypted": encrypted,
                    "data_file": data_name,
                    "feedback_repository": feedback_repository,
                }
            ),
        )
        _write(stage / "index.html", _HTML)
        _write(assets_directory / "app.js", _APP_JS)
        _write(assets_directory / "site.css", _CSS)
        _replace_directory(stage, output_directory)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return SiteBuildResult(output_directory, encrypted, len(recommendations.recommendations))


def _replace_directory(stage: Path, output_directory: Path) -> None:
    backup = output_directory.with_name(f".{output_directory.name}.previous")
    if backup.exists():
        shutil.rmtree(backup)
    if output_directory.exists():
        os.replace(output_directory, backup)
    try:
        os.replace(stage, output_directory)
    except BaseException:
        if backup.exists():
            os.replace(backup, output_directory)
        raise
    shutil.rmtree(backup, ignore_errors=True)


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _encrypt_payload(payload: dict[str, object], passphrase: str | None) -> str:
    if passphrase is None:
        raise AssertionError("encrypted output requires a passphrase")
    return encrypt_json(payload, passphrase).to_json()


def _valid_repository(value: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value) is not None


_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Zotero arXiv Daily</title>
  <link rel="stylesheet" href="assets/site.css">
</head>
<body>
  <a class="skip-link" href="#recommendations">Skip to recommendations</a>
  <header><h1>Zotero arXiv Daily</h1><p id="status" role="status">Loading recommendations…</p></header>
  <main>
    <section id="controls" aria-label="Recommendation filters" hidden>
      <label>Date <input id="date-filter" type="date"></label>
      <label>Topic <input id="topic-filter" type="search" placeholder="Search title or summary"></label>
      <label>Category <select id="category-filter"><option value="">All categories</option></select></label>
      <label>Source <select id="source-filter"><option value="">All sources</option><option value="core">Core</option><option value="adjacent">Adjacent</option><option value="exploration">Exploration</option></select></label>
      <label>Feedback <select id="feedback-filter"><option value="">All feedback</option><option value="interested">Interested</option><option value="not_interested">Not interested</option><option value="save_for_later">Save for later</option><option value="read">Read</option></select></label>
    </section>
    <section id="recommendations" aria-live="polite"></section>
    <section id="feedback-export" hidden>
      <h2>Send feedback</h2>
      <p>Your feedback stays in this browser until you explicitly submit it.</p>
      <a id="feedback-issue" target="_blank" rel="noopener">Create one feedback issue</a>
      <button id="feedback-confirm" type="button">I submitted feedback</button>
    </section>
  </main>
  <noscript><p class="error">JavaScript is required to decrypt protected recommendations. General arXiv links remain available at <a href="https://arxiv.org/">arXiv.org</a>.</p></noscript>
  <script type="module" src="assets/app.js"></script>
</body>
</html>
"""

_CSS = """* { box-sizing: border-box; } body { color: #17202a; font: 1rem/1.5 system-ui, sans-serif; margin: 0 auto; max-width: 70rem; padding: 1rem; } header { border-bottom: 1px solid #ccd1d1; } #controls { display: grid; gap: .75rem; grid-template-columns: repeat(auto-fit, minmax(12rem, 1fr)); margin: 1rem 0; } label { display: grid; font-weight: 600; gap: .25rem; } input, select, button { font: inherit; padding: .4rem; } article { border: 1px solid #ccd1d1; border-radius: .5rem; margin: 1rem 0; padding: 1rem; } .meta { color: #566573; } .actions { display: flex; flex-wrap: wrap; gap: .5rem; } .skip-link { left: -9999px; position: absolute; } .skip-link:focus { background: white; left: 1rem; padding: .5rem; top: 1rem; } .error { color: #922b21; font-weight: 600; } button:focus, a:focus, input:focus, select:focus { outline: 3px solid #2874a6; outline-offset: 2px; } @media (max-width: 40rem) { body { padding: .75rem; } }"""

_APP_JS = r"""const storageKey = "zotero-arxiv-daily-feedback-v1";
const actions = ["interested", "not_interested", "save_for_later", "read"];
const state = { data: null, feedback: loadFeedback() };
const status = document.querySelector("#status");
const list = document.querySelector("#recommendations");
const controls = document.querySelector("#controls");
const exportPanel = document.querySelector("#feedback-export");

function loadFeedback() { try { const value = JSON.parse(localStorage.getItem(storageKey) || "{}"); return value.schema_version === 1 && value.actions ? value.actions : {}; } catch { return {}; } }
function saveFeedback() { localStorage.setItem(storageKey, JSON.stringify({schema_version: 1, actions: state.feedback})); updateFeedbackExport(); render(); }
function escapeText(value) { return String(value); }
function filters() { return { date: document.querySelector("#date-filter").value, topic: document.querySelector("#topic-filter").value.toLowerCase(), category: document.querySelector("#category-filter").value, source: document.querySelector("#source-filter").value, feedback: document.querySelector("#feedback-filter").value }; }
function matches(item, filter) { const feedback = state.feedback[item.arxiv_id]?.action || ""; return (!filter.date || item.published_on === filter.date) && (!filter.topic || `${item.title} ${item.summary} ${item.reason}`.toLowerCase().includes(filter.topic)) && (!filter.category || item.categories.includes(filter.category)) && (!filter.source || item.quota_source === filter.source) && (!filter.feedback || feedback === filter.feedback); }
function render() { if (!state.data) return; const filter = filters(); list.replaceChildren(...state.data.recommendations.filter(item => matches(item, filter)).map(card)); status.textContent = `${list.children.length} recommendation(s) shown.`; }
function card(item) { const article = document.createElement("article"); const heading = document.createElement("h2"); const link = document.createElement("a"); link.href = item.abstract_url; link.textContent = escapeText(item.title); heading.append(link); article.append(heading); const meta = document.createElement("p"); meta.className = "meta"; meta.textContent = `${item.authors.join(", ")} · ${item.categories.join(", ")} · ${item.published_on} · ${item.quota_source} · ${(item.confidence * 100).toFixed(0)}% confidence`; article.append(meta); for (const [name, text] of [["Summary", item.summary], ["Why recommended", item.reason]]) { const title = document.createElement("h3"); title.textContent = name; const body = document.createElement("p"); body.textContent = escapeText(text); article.append(title, body); } const pdf = document.createElement("a"); pdf.href = item.pdf_url; pdf.textContent = "PDF"; article.append(pdf); const group = document.createElement("div"); group.className = "actions"; for (const action of actions) { const button = document.createElement("button"); button.type = "button"; button.textContent = action.replaceAll("_", " "); button.setAttribute("aria-pressed", String(state.feedback[item.arxiv_id]?.action === action)); button.addEventListener("click", () => { state.feedback[item.arxiv_id] = {action, updated_at: new Date().toISOString()}; saveFeedback(); }); group.append(button); } article.append(group); return article; }
function updateCategoryOptions() { const select = document.querySelector("#category-filter"); const categories = [...new Set(state.data.recommendations.flatMap(item => item.categories))].sort(); select.replaceChildren(new Option("All categories", ""), ...categories.map(value => new Option(value, value))); }
function issueUrl(repository) { const feedback = Object.entries(state.feedback).map(([arxiv_id, value]) => ({arxiv_id, action: value.action, updated_at: value.updated_at})); const body = JSON.stringify({schema_version: 1, feedback}); if (body.length > 6000) throw new Error("Feedback payload exceeds the issue size limit; submit fewer pending actions."); return `https://github.com/${repository}/issues/new?title=${encodeURIComponent("Zotero arXiv Daily feedback")}&labels=${encodeURIComponent("zotero-feedback")}&body=${encodeURIComponent(body)}`; }
function updateFeedbackExport() { const repository = state.config.feedback_repository; const entries = Object.keys(state.feedback); exportPanel.hidden = !repository || entries.length === 0; if (!exportPanel.hidden) { try { document.querySelector("#feedback-issue").href = issueUrl(repository); } catch (error) { status.textContent = error.message; } } }
async function decrypt(envelope, passphrase) { const bytes = value => Uint8Array.from(atob(value), char => char.charCodeAt(0)); const material = await crypto.subtle.importKey("raw", new TextEncoder().encode(passphrase), "PBKDF2", false, ["deriveKey"]); const key = await crypto.subtle.deriveKey({name:"PBKDF2", salt:bytes(envelope.salt), iterations:envelope.iterations, hash:"SHA-256"}, material, {name:"AES-GCM", length:256}, false, ["decrypt"]); const plain = await crypto.subtle.decrypt({name:"AES-GCM", iv:bytes(envelope.nonce)}, key, bytes(envelope.ciphertext)); return JSON.parse(new TextDecoder().decode(plain)); }
async function main() { try { const config = await (await fetch("data/site-config.json", {cache:"no-store"})).json(); state.config = config; const raw = await (await fetch(`data/${config.data_file}`, {cache:"no-store"})).text(); if (config.encrypted) { const passphrase = window.prompt("Enter the recommendation passphrase"); if (!passphrase) throw new Error("A passphrase is required to display protected recommendations."); state.data = await decrypt(JSON.parse(raw), passphrase); } else { state.data = JSON.parse(raw); } if (state.data.schema_version !== 1 || !Array.isArray(state.data.recommendations)) throw new Error("Recommendation data has an unsupported schema."); controls.hidden = false; updateCategoryOptions(); for (const input of controls.querySelectorAll("input, select")) input.addEventListener("input", render); document.querySelector("#feedback-confirm").addEventListener("click", () => { state.feedback = {}; saveFeedback(); }); updateFeedbackExport(); render(); } catch (error) { status.textContent = "Recommendations could not be loaded safely."; const message = document.createElement("p"); message.className = "error"; message.textContent = `${error.message} General arXiv links remain available at arXiv.org.`; const link = document.createElement("a"); link.href = "https://arxiv.org/"; link.textContent = "Open arXiv.org"; list.replaceChildren(message, link); } }
main();
"""
