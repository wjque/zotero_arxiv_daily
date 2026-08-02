"""Build a self-contained, responsive static recommendation site."""

# ruff: noqa: E501

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime
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
    if recommendations.schema_version == 2:
        recommendations = replace(recommendations, artifact_built_at=datetime.now(UTC).isoformat())
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
                    "schema_version": 2,
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
  <header class="masthead"><div><p class="eyebrow">Personal research briefing</p><h1>Zotero arXiv Daily</h1></div><p id="status" role="status">Loading recommendations…</p></header>
  <main>
    <section id="batch-status" class="status-panel" aria-labelledby="batch-heading" hidden><div><p class="eyebrow" id="last-success-label">Last successful batch</p><h2 id="batch-heading">Batch status</h2></div><dl id="batch-details"></dl></section>
    <section id="controls" class="controls" aria-label="Recommendation filters" hidden>
      <label><span data-label="date">Date</span><input id="date-filter" type="date"></label>
      <label><span data-label="topic">Topic</span><input id="topic-filter" type="search" placeholder="Search title or summary"></label>
      <label><span data-label="category">Category</span><select id="category-filter"></select></label>
      <label><span data-label="source">Source</span><select id="source-filter"></select></label>
      <label><span data-label="feedback">Feedback</span><select id="feedback-filter"></select></label>
    </section>
    <section id="recommendations" class="cards" aria-live="polite"></section>
    <section id="feedback-export" class="export-panel" hidden>
      <h2 id="feedback-heading">Send feedback</h2><p id="feedback-privacy">Your feedback stays in this browser until you explicitly submit it.</p>
      <div class="panel-actions"><a id="feedback-issue" class="button primary" target="_blank" rel="noopener">Create one feedback issue</a><button id="feedback-confirm" type="button">I submitted feedback</button></div>
    </section>
  </main>
  <noscript><p class="error">JavaScript is required to decrypt protected recommendations. General arXiv links remain available at <a href="https://arxiv.org/">arXiv.org</a>.</p></noscript>
  <script type="module" src="assets/app.js"></script>
</body>
</html>
"""

_CSS = """:root{color-scheme:light dark;--bg:#f5f7fb;--surface:#fff;--surface2:#eef2f7;--text:#172033;--muted:#566176;--line:#cbd3df;--accent:#1559b7;--accentText:#fff;--danger:#a12622;--shadow:0 8px 24px rgb(30 48 75/.08);--radius:.8rem}*{box-sizing:border-box}html{background:var(--bg)}body{color:var(--text);font:1rem/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;margin:0 auto;max-width:76rem;padding:clamp(.75rem,2vw,1.5rem)}a{color:var(--accent)}.masthead{align-items:end;border-bottom:1px solid var(--line);display:flex;flex-wrap:wrap;gap:1rem;justify-content:space-between;padding:.5rem 0 1.25rem}h1{font-size:clamp(1.75rem,5vw,2.7rem);letter-spacing:-.035em;margin:.1rem 0}h2{font-size:clamp(1.2rem,3vw,1.55rem);line-height:1.25}h3{font-size:1rem;margin:1rem 0 .25rem}.eyebrow{color:var(--muted);font-size:.75rem;font-weight:750;letter-spacing:.1em;margin:0;text-transform:uppercase}#status,.meta{color:var(--muted)}.status-panel,.export-panel{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);margin:1.25rem 0;padding:clamp(1rem,3vw,1.5rem)}.status-panel{border-left:.4rem solid #24845b;display:grid;gap:1rem;grid-template-columns:minmax(10rem,.7fr) 2fr}.status-panel.stale{border-left-color:#c47a10}dl{display:grid;gap:.75rem;grid-template-columns:repeat(auto-fit,minmax(10rem,1fr));margin:0}dt{color:var(--muted);font-size:.78rem;font-weight:700}dd{margin:.1rem 0 0;overflow-wrap:anywhere}.controls{background:var(--surface2);border-radius:var(--radius);display:grid;gap:.75rem;grid-template-columns:repeat(auto-fit,minmax(min(100%,11rem),1fr));margin:1.25rem 0;padding:1rem}label{display:grid;font-size:.85rem;font-weight:700;gap:.3rem}input,select,button,.button{border:1px solid var(--line);border-radius:.45rem;font:inherit;min-height:2.75rem;padding:.55rem .7rem}input,select,button{background:var(--surface);color:var(--text)}button,.button{cursor:pointer;text-align:center}.button{align-items:center;display:inline-flex;justify-content:center;text-decoration:none}.primary,.actions button[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:var(--accentText)}.cards{display:grid;gap:1rem}article{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);min-width:0;padding:clamp(1rem,3vw,1.5rem)}article h2{margin:.25rem 0 .7rem;overflow-wrap:anywhere}.badges,.actions,.panel-actions{display:flex;flex-wrap:wrap;gap:.5rem}.badge{background:var(--surface2);border:1px solid var(--line);border-radius:99rem;font-size:.78rem;padding:.2rem .55rem}.actions{border-top:1px solid var(--line);margin-top:1rem;padding-top:1rem}.skip-link{left:-9999px;position:absolute}.skip-link:focus{background:var(--surface);left:1rem;padding:.5rem;top:1rem;z-index:2}.error{color:var(--danger);font-weight:650}:focus-visible{outline:3px solid #2b78d0;outline-offset:3px}@media(prefers-color-scheme:dark){:root{--bg:#10141d;--surface:#191f2b;--surface2:#222a38;--text:#edf2fa;--muted:#b5c0d1;--line:#3b4658;--accent:#82b4ff;--accentText:#101722;--danger:#ff9c96;--shadow:none}}@media(max-width:40rem){.status-panel{grid-template-columns:1fr}.panel-actions>*{width:100%}}@media(prefers-reduced-motion:reduce){*,*::before,*::after{scroll-behavior:auto!important;transition:none!important}}"""

_APP_JS = r"""const storageKey="zotero-arxiv-daily-feedback-v1",actions=["interested","not_interested","save_for_later","read"];
const catalogs={en:{loading:"Loading recommendations…",shown:n=>`${n} recommendation(s) shown.`,empty:"No recommendations match these filters.",last:"Last successful batch",batch:"Batch status",started:"Started",completed:"Completed",built:"Artifact built",profile:"Zotero library version",run:"Workflow run",legacy:"Legacy batch; completion time unavailable",fresh:"Fresh",stale:h=>`Stale · ${h} hours old`,date:"Date",topic:"Topic",category:"Category",source:"Source",feedback:"Feedback",allCategories:"All categories",allSources:"All sources",allFeedback:"All feedback",summary:"Summary",reason:"Why recommended",confidence:"confidence",signals:{watched_author:"Watched author",watched_institution:"Watched institution"},send:"Send feedback",privacy:"Your feedback stays in this browser until you explicitly submit it.",issue:"Create one feedback issue",confirm:"I submitted feedback",passphrase:"Enter the recommendation passphrase",required:"A passphrase is required to display protected recommendations.",unsafe:"Recommendations could not be loaded safely.",open:"Open arXiv.org"},"zh-CN":{loading:"正在加载推荐…",shown:n=>`显示 ${n} 篇推荐。`,empty:"没有符合当前筛选条件的推荐。",last:"最近一次成功批次",batch:"批次状态",started:"开始时间",completed:"完成时间",built:"构建时间",profile:"Zotero 文库版本",run:"工作流运行",legacy:"旧版批次；完成时间不可用",fresh:"数据新鲜",stale:h=>`数据已陈旧 · ${h} 小时`,date:"日期",topic:"主题",category:"分类",source:"来源",feedback:"反馈",allCategories:"全部分类",allSources:"全部来源",allFeedback:"全部反馈",summary:"摘要",reason:"推荐理由",confidence:"置信度",signals:{watched_author:"关注作者",watched_institution:"关注机构"},send:"发送反馈",privacy:"反馈仅保存在此浏览器中，直到你明确提交。",issue:"创建一个反馈 Issue",confirm:"我已提交反馈",passphrase:"请输入推荐页面口令",required:"必须提供口令才能显示受保护的推荐。",unsafe:"无法安全加载推荐。",open:"打开 arXiv.org"}};
const state={data:null,config:null,feedback:loadFeedback(),locale:"en",t:catalogs.en},status=document.querySelector("#status"),list=document.querySelector("#recommendations"),controls=document.querySelector("#controls"),exportPanel=document.querySelector("#feedback-export");
function loadFeedback(){try{const value=JSON.parse(localStorage.getItem(storageKey)||"{}");return value.schema_version===1&&value.actions?value.actions:{}}catch{return {}}}function saveFeedback(){localStorage.setItem(storageKey,JSON.stringify({schema_version:1,actions:state.feedback}));updateFeedbackExport();render()}
function filters(){return{date:document.querySelector("#date-filter").value,topic:document.querySelector("#topic-filter").value.toLowerCase(),category:document.querySelector("#category-filter").value,source:document.querySelector("#source-filter").value,feedback:document.querySelector("#feedback-filter").value}}
function matches(item,f){const feedback=state.feedback[item.arxiv_id]?.action||"";return(!f.date||item.published_on===f.date)&&(!f.topic||`${item.title} ${item.summary} ${item.reason}`.toLowerCase().includes(f.topic))&&(!f.category||item.categories.includes(f.category))&&(!f.source||item.quota_source===f.source)&&(!f.feedback||feedback===f.feedback)}
function render(){if(!state.data)return;const items=state.data.recommendations.filter(item=>matches(item,filters()));list.replaceChildren(...items.map(card));if(!items.length){const empty=document.createElement("p");empty.textContent=state.t.empty;list.append(empty)}status.textContent=state.t.shown(items.length)}
function badge(text){const span=document.createElement("span");span.className="badge";span.textContent=text;return span}function card(item){const article=document.createElement("article"),heading=document.createElement("h2"),link=document.createElement("a");link.href=item.abstract_url;link.textContent=String(item.title);heading.append(link);article.append(heading);const badges=document.createElement("div");badges.className="badges";for(const value of [...item.categories,item.quota_source,`${(item.confidence*100).toFixed(0)}% ${state.t.confidence}`,...(item.preference_signals||[]).map(value=>state.t.signals[value])])badges.append(badge(value));article.append(badges);const meta=document.createElement("p");meta.className="meta";meta.textContent=`${item.authors.join(", ")} · ${item.published_on}`;article.append(meta);for(const[name,text]of[[state.t.summary,item.summary],[state.t.reason,item.reason]]){const title=document.createElement("h3"),body=document.createElement("p");title.textContent=name;body.textContent=String(text);article.append(title,body)}const pdf=document.createElement("a");pdf.href=item.pdf_url;pdf.textContent="PDF";article.append(pdf);const group=document.createElement("div");group.className="actions";for(const action of actions){const button=document.createElement("button");button.type="button";button.textContent=action.replaceAll("_"," ");button.setAttribute("aria-pressed",String(state.feedback[item.arxiv_id]?.action===action));button.addEventListener("click",()=>{state.feedback[item.arxiv_id]={action,updated_at:new Date().toISOString()};saveFeedback()});group.append(button)}article.append(group);return article}
function selectOptions(){const categories=[...new Set(state.data.recommendations.flatMap(item=>item.categories))].sort();document.querySelector("#category-filter").replaceChildren(new Option(state.t.allCategories,""),...categories.map(value=>new Option(value,value)));document.querySelector("#source-filter").replaceChildren(new Option(state.t.allSources,""),...['core','adjacent','exploration'].map(value=>new Option(value,value)));document.querySelector("#feedback-filter").replaceChildren(new Option(state.t.allFeedback,""),...actions.map(value=>new Option(value.replaceAll("_"," "),value)))}
function localTime(value){return new Intl.DateTimeFormat(state.locale,{dateStyle:"medium",timeStyle:"short",timeZone:"Asia/Shanghai"}).format(new Date(value))}function detail(term,value,node){const wrap=document.createElement("div"),dt=document.createElement("dt"),dd=document.createElement("dd");dt.textContent=term;if(node)dd.append(node);else dd.textContent=value;wrap.append(dt,dd);return wrap}
function batchStatus(){const panel=document.querySelector("#batch-status"),details=document.querySelector("#batch-details"),data=state.data;panel.hidden=false;document.querySelector("#last-success-label").textContent=state.t.last;document.querySelector("#batch-heading").textContent=state.t.batch;const started=data.generation_started_at||data.generated_at,completed=data.generation_completed_at;details.replaceChildren(detail(state.t.started,localTime(started)),detail(state.t.completed,completed?localTime(completed):state.t.legacy));if(data.artifact_built_at)details.append(detail(state.t.built,localTime(data.artifact_built_at)));if(data.profile_library_version)details.append(detail(state.t.profile,String(data.profile_library_version)));if(data.workflow_run){const link=document.createElement("a");link.href=data.workflow_run.run_url;link.textContent=`#${data.workflow_run.run_id} · ${data.workflow_run.source_revision.slice(0,7)}`;details.append(detail(state.t.run,"",link))}if(completed){const hours=Math.max(0,Math.floor((Date.now()-new Date(completed))/36e5));panel.classList.toggle("stale",hours>=36);panel.dataset.ageHours=String(hours);status.textContent=hours>=36?state.t.stale(hours):state.t.fresh}}
function localize(){document.documentElement.lang=state.locale;for(const node of document.querySelectorAll("[data-label]"))node.textContent=state.t[node.dataset.label];document.querySelector("#feedback-heading").textContent=state.t.send;document.querySelector("#feedback-privacy").textContent=state.t.privacy;document.querySelector("#feedback-issue").textContent=state.t.issue;document.querySelector("#feedback-confirm").textContent=state.t.confirm}
function issueUrl(repository){const feedback=Object.entries(state.feedback).map(([arxiv_id,value])=>({arxiv_id,action:value.action,updated_at:value.updated_at})),body=JSON.stringify({schema_version:1,feedback});if(body.length>6000)throw new Error("Feedback payload exceeds the issue size limit.");return`https://github.com/${repository}/issues/new?title=${encodeURIComponent("Zotero arXiv Daily feedback")}&labels=zotero-feedback&body=${encodeURIComponent(body)}`}
function updateFeedbackExport(){const repository=state.config.feedback_repository,entries=Object.keys(state.feedback);exportPanel.hidden=!repository||!entries.length;if(!exportPanel.hidden)document.querySelector("#feedback-issue").href=issueUrl(repository)}async function decrypt(envelope,passphrase){const bytes=value=>Uint8Array.from(atob(value),char=>char.charCodeAt(0)),material=await crypto.subtle.importKey("raw",new TextEncoder().encode(passphrase),"PBKDF2",false,["deriveKey"]),key=await crypto.subtle.deriveKey({name:"PBKDF2",salt:bytes(envelope.salt),iterations:envelope.iterations,hash:"SHA-256"},material,{name:"AES-GCM",length:256},false,["decrypt"]),plain=await crypto.subtle.decrypt({name:"AES-GCM",iv:bytes(envelope.nonce)},key,bytes(envelope.ciphertext));return JSON.parse(new TextDecoder().decode(plain))}
async function main(){try{state.config=await(await fetch("data/site-config.json",{cache:"no-store"})).json();const raw=await(await fetch(`data/${state.config.data_file}`,{cache:"no-store"})).text();if(state.config.encrypted){const passphrase=window.prompt(state.t.passphrase);if(!passphrase)throw new Error(state.t.required);state.data=await decrypt(JSON.parse(raw),passphrase)}else state.data=JSON.parse(raw);if(![1,2].includes(state.data.schema_version)||!Array.isArray(state.data.recommendations))throw new Error("Unsupported recommendation schema.");state.locale=state.data.output_language==="zh-CN"?"zh-CN":"en";state.t=catalogs[state.locale];localize();controls.hidden=false;selectOptions();batchStatus();for(const input of controls.querySelectorAll("input,select"))input.addEventListener("input",render);document.querySelector("#feedback-confirm").addEventListener("click",()=>{state.feedback={};saveFeedback()});updateFeedbackExport();render()}catch(error){status.textContent=state.t.unsafe;const message=document.createElement("p"),link=document.createElement("a");message.className="error";message.textContent=String(error.message);link.href="https://arxiv.org/";link.textContent=state.t.open;list.replaceChildren(message,link)}}main();
"""
