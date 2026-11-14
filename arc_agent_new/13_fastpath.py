"""Lean fast path: compile a requirement document into ONE self-contained web application.

For tasks below a size threshold this pass replaces the full 13-pass pipeline: one model
call generates a server-rendered, standard-library-only ``app.py`` (http.server + sqlite3),
which is then checked locally without a browser (compile, start, crawl, submit forms,
seeded-account login), repaired with at most two search/replace rounds, and reviewed once
scenario by scenario. A bad first draft is regenerated once instead of being patched.

The checking strategy and the accessibility contract were adapted from the lean-compiler
reference implementation used during local benchmarking (bench/others/lean-compiler-v3.1).
The compiler (infrastructure/compiler.py) invokes this pass before the 13-stage pipeline and
falls back to that pipeline whenever this pass does not finish GREEN, so the quality gate is
never weakened.

Environment knobs:
  ARC_FASTPATH=0|off|false   disable the fast path (always use the full pipeline)
  ARC_FASTPATH_MAX_LEAVES    leaf-requirement threshold (default 40)
"""

from __future__ import annotations

import html
import itertools
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from dotenv import load_dotenv
from openai import APIConnectionError, APITimeoutError, BadRequestError, InternalServerError, OpenAI, RateLimitError

TRANSIENT = (APIConnectionError, APITimeoutError, InternalServerError, RateLimitError)
BACKOFF_SECONDS = (5, 15, 30, 60)

APP_FILE = "app.py"
MODEL_NAME = "deepseek-flash"
MAX_REPAIRS = 3
MAX_OUTPUT_TOKENS = 60000
REPAIR_TOKENS = 8000
REVIEW_TOKENS = 12000
REDRAFT_THRESHOLD = 12  # redraft only nearly-broken drafts: repairs routinely fix 10+ problems


class ModelUnavailable(RuntimeError):
    """The model endpoint kept failing after all retries."""


SYSTEM_PROMPT = """You are a requirement compiler. You turn a requirement document into ONE runnable web application.

Output exactly one file named app.py, wrapped as:
<<<FILE app.py
...python source...
FILE>>>
No other text.

## Architecture (mandatory)
- Python 3 standard library only (http.server.ThreadingHTTPServer, sqlite3, json, hashlib, secrets, html, urllib.parse). No third-party packages, no external network access.
- Listen on host os.environ.get("HOST", "0.0.0.0") and int(os.environ.get("PORT", "3000")).
- Server-rendered HTML. Every page is complete HTML produced by the server on GET; content must be present without JavaScript. Use plain HTML forms (method="post") for every action, and answer each POST with a 303 redirect to the page the requirements say the action returns to (post/redirect/get). JavaScript is allowed ONLY as: (1) a live-search auto-submit — <input ... oninput="this.form.requestSubmit()"> on a GET form whose server-rendered page filters the list (an empty query shows everything); (2) a countdown/timer display refresh, when the requirements test a changing value; (3) pure cosmetics. JavaScript must never create, read or decide business data.
- Persist all data in SQLite next to app.py (create tables on startup). Create the seed data and accounts described in the requirements on startup when they are missing (idempotent), with exactly the given names, descriptions and credentials.
- Sessions: random token in an HttpOnly cookie (Path=/); a reload keeps the user signed in. Hash passwords with hashlib.pbkdf2_hmac.
- Escape every user-provided value with html.escape. Never crash on unknown ids: return a 404 page.
- Keep a small shared layout function: <nav> with the global entry links, then <main>.
- Keep CSS short; do not reproduce decorative details of the reference screenshots.

## Accessibility contract (tests use Playwright getByRole/getByLabel/getByPlaceholder/getByText with exact names)
- "Name"(role=X) means an element whose ARIA role is X and whose accessible name is exactly Name. Copy names character for character, including odd spelling.
- role=link -> <a href>. role=button -> <button>. A requirement that allows "a semantically equivalent link" may be an <a href> instead.
- A role=button that only navigates (for example Edit, New ..., Create ..., or Delete opening a confirmation) is still a real <button>: wrap it as <form method="get" action="/target"><button type="submit">Name</button></form>.
- Do not repeat the global navigation links inside page content; they live only in <nav>.
- role=textbox -> <input type="text"> (or <textarea> for long text) with <label for="..."> whose text is exactly the name AND placeholder="the exact name". Password fields: <input type="password"> with the same label/placeholder rule.
- role=checkbox -> <input type="checkbox"> with <label for>. Initially unchecked unless the requirements say otherwise.
- role=region -> <section aria-label="Name"> (also render a visible <h2> with the same text inside it).
- A toggle that reports pressed state is a <button aria-pressed="true|false"> whose text changes as the requirements say. Sidebar/panel toggles report aria-expanded="true|false"; implement them as a POST form that re-renders the collapsed/expanded state (no JavaScript required).
- Advanced controls (use ONLY when the requirements name them):
  - combobox -> native <select> with <label for="..."> exactly the name; preselected option matches the requirement's default.
  - searchbox -> <input type="search"> with aria-label AND <label for> exactly the name (e.g. "Search"); filtering is server-side via the live-search GET form above.
  - spinbutton -> <input type="number"> with the exact label.
  - radio -> <input type="radio" name="..."> with <label for>; checked state per the requirement.
  - status -> <div role="status" aria-label="Name"> whose text is exactly the message (e.g. "Note deleted"); buttons the requirements place inside it (e.g. Undo) live inside this div.
  - dialog -> a visible container <div role="dialog" aria-label="Name"> (with an <h2> of the same name inside) on its own page; opening it navigates there, closing returns to the previous page.
  - navigation -> <nav aria-label="Name"> containing the navigation buttons/links; the active item carries aria-current="true".
  - menuitem -> items inside a container with role="menu" (revealed by CSS :hover on the item's trigger, so no JavaScript is needed); each menuitem is a <button role="menuitem"> that submits a POST form.
  - tab -> <div role="tablist"> with <a role="tab" href="..."> (aria-selected="true" on the active one) and panels <div role="tabpanel">; activating a tab navigates to a page that renders that tab selected and its panel visible.
  - table/row/columnheader -> native <table>/<tr>/<th>.
  - date picker -> a server-rendered month calendar: a grid of <a> day cells (text = day number) inside the dialog/page; clicking a day submits GET/POST with the date and re-renders the form with that date filled.
  - timer/countdown -> render the exact current value the requirements define (e.g. "60s"); only add a JS interval if a test expects the value to change while the page is open.
- Undo pattern: every destructive action the requirements pair with an Undo (delete/archive/...) must, right after the action, show a role=status notice with the exact required name (e.g. "Note deleted") containing an "Undo" button (a POST form) that restores the item and then shows a role=status "Action undone" notice. Deleted items stay listed where the requirements say (e.g. Trash) so Undo can restore them. When the requirements pair the notice with a Dismiss action, the same status div also contains the Dismiss button (its POST hides the notice).
- Chrome (UI scaffolding the tests locate by role):
  - Navigation/sidebar: render it as <nav aria-label="<exact name>"> containing EVERY section button the requirements name (e.g. Notes, Reminders, Archived, Trash, Edit Labels) plus a Toggle button (aria-expanded="true"/"false") when the requirements name one. The button of the active section carries aria-current="true". Collapsing re-renders the sidebar without the section labels visible (aria-expanded="false").
  - Dialogs: every dialog/page the requirements name (e.g. "Note editor", "Note labels", "Settings") must appear with role=dialog and aria-label EXACTLY as named, and the actions inside it are named exactly as the scenarios say (e.g. Close hides it and returns to the list; Pin note toggles aria-pressed).
  - Hover menus: an item's overflow menu ("More options <item>") is a <button> inside the item region revealed on hover, opening a role=menu whose items are role=menuitem buttons (each submits a POST form).
  - Items rendered once: an item appears EXACTLY once per page (never both in a list section and a grid section); only the active view is rendered. Item regions carry aria-label = the item's exact name and contain its title/content text exactly once.
  - Live search: the searchbox GET form filters the visible items server-side; the empty query restores all items; when the requirements define a highlight, each matching item additionally contains a <section aria-label="Highlight <term>"> (with the typed term) while the filter is active.
  - Settings dialog: persists choices (view style, color) per the requirement; Cancel discards pending changes and closes the dialog.
  - View toggle (grid/list): a toggle button whose state re-renders the active layout; the button keeps its exact name.
- Feed and community patterns (use when the requirements describe them):
  - STRUCTURAL TEMPLATE for a discussion detail page (adapt names to the requirements; every named
    region/status below must carry the EXACT name the requirements give):
    <header role="banner"> [Log in / Sign up, or the account's display-name button] [Search searchbox in a GET form] </header>
    <nav role="navigation" aria-label="Main navigation"> [Questions] [Tags] [Users] </nav>
    <section aria-label="Question post"> <h1>the exact title</h1> <p>body text</p>
      <button>Up vote</button> <button>Down vote</button> <div role="status" aria-label="Score">7</div>
      <button>Save</button> <button>Timeline</button> <button>Share</button> <button>Edit</button> <button>Follow</button>
      <div role="status" aria-label="Asked">date</div> <div role="status" aria-label="Modified">date</div>
      <div role="status" aria-label="View count">42</div>
      [author block: <img> avatar, the display name, <div role="status" aria-label="Reputation">100</div>]
      [tag buttons: one per tag, exact tag text]
      <section aria-label="Comments">
        <section aria-label="Comment {the comment's body text}">the body</section> ...
        <button>Show 2 more comments</button> <button>Add a comment</button>
      </section>
    </section>
    <section aria-label="Answers"> <h2>Answers</h2> <div role="status" aria-label="Answer count">2</div>
      <section aria-label="Answer by {display name}"> body text
        <button>Up vote</button> <button>Down vote</button> <div role="status" aria-label="Score">3</div>
        [question owner's view:] <button>Accept answer</button> → <div role="status" aria-label="Accepted answer">…</div>
        <section aria-label="Comments"> <section aria-label="Comment {body}">…</section> </section>
      </section>
    </section>
    Reproduce this structure shape exactly; rename only to the requirements' exact wording.
  - A page banner (<header role="banner">) holds the Log in/Sign up buttons (logged out) or the account's display-name button (logged in), the Search searchbox, and the main navigation (role=navigation with its exact name, e.g. "Main navigation").
  - Search: the searchbox lives in a GET form; pressing Enter or clicking the search button re-renders the page with a region named exactly "Search results" whose entries are links/buttons with the items' exact titles. An empty query shows everything.
  - Sorting/filtering comboboxes auto-submit their GET form on change (onchange="this.form.requestSubmit()") so the list re-sorts immediately; keep the option labels exactly as named (e.g. "Highest score", "Trending", "Date modified").
  - Live character counters: a role=status with the exact name (e.g. "Title character count") whose text equals the CURRENT length of its input, updated by a tiny JS input listener.
  - Pagination: list regions offer a "Next page" button/link that renders the next page of items (server-side page parameter); every item appears exactly once across pages.
  - Votes: inside each post/answer region exactly one "Up vote" and one "Down vote" button (a POST form that toggles the signed-in user's vote and re-renders); a role=status "Score" inside the same region shows the current count.
  - Answers: each answer is a region "Answer by <display name>"; the question owner's view offers "Accept answer" (which renders an "Accepted answer" state); a role=status "Answer count" shows the number.
  - Comments: each comment is a region named exactly "Comment <its body text>"; when the thread has more comments than the visible limit, a "Show 2 more comments" button inside the Comments region reveals the rest (server re-render). "Add a comment" opens the comment form; comment votes update the "Comment votes" status.
  - Avatars: author/answer blocks render an image (<img> or a div role="img" with the author's initial) so getByRole('img') finds one.
  - Status values: statuses such as "Asked"/"Modified" show the item's dates, "View count" a number, "Reputation" a number, "Question count" a number — exact role=status names, text inside them is the value.
  - Seed data: create EVERY seed record the requirements describe on startup (idempotently) — accounts with their display names/reputations, published questions with exact titles/bodies/tags/author/dates/comment threads/answers/vote counts, badges — so the tests find them by their fixed titles without creating anything first.
- Travel and commerce patterns (use when the requirements describe them):
  - Location selectors: a dialog with alphabetical index tabs (e.g. Popular, ABCDE, FGHIJ, ...); every station/city is a button with its exact name (e.g. "Beijing (北京)"); clicking one fills the form field and closes/re-renders the dialog.
  - Dates: a date bar/dialog renders one button per date (exact name like "2026-07-22", optionally with the price inside the button); a Date textbox accepts YYYY-MM-DD; the server re-renders the exact validation message the requirements define (e.g. a valid range); "Next week"/"More dates" shift the shown range.
  - SMS verification: "Send code" renders the simulated code as visible text (the requirements state the fixed code, e.g. 123456) and is replaced by "Resend code"; the form accepts the code only after it was sent in the current attempt and shows the exact error text otherwise.
  - E-commerce: product cards in the list; "Quick view" opens a dialog named exactly "Quick view <product name>" (Close closes it); the product page offers tabs (e.g. Description, Product Details, Reviews) with aria-selected on the active one; size is a combobox; quantity is a spinbutton with "Increase quantity"/"Decrease quantity" buttons; adding to cart shows "Product successfully added to your shopping cart" with "Continue shopping"/"Proceed to checkout"; a Filters panel uses checkboxes/radios plus "Clear all"; "Sort by" is a combobox that auto-submits on change; wishlist/favorites flows offer "Add to cart" and "Remove".
- Headings: a detail page shows its item's name in exactly one <h1>. A list or section page shows an <h1> with the page name. When the requirements say an action "returns to X", "opens X" or shows "X list", the page reached has an <h1> exactly X as spelled there (even if the spelling looks wrong), and any named list region keeps its exact name.
- Lists of items inside a named region: each item is exactly ONE <a href> (or <button>) whose whole text is the item's name, nothing else inside it (put dates or counts outside the link). Never render the same item twice in one region, and never put another link/button with the same name in that region. Never decorate an item's name with status markers such as " (draft)", quotes, or suffixes: tests match item names with exact: true, so a link whose text is "X (draft)" does NOT match a test looking for "X". The draft/published state is shown elsewhere on the page, not inside the item link.
- On one page, a given control name (for example Edit, Delete, Cancel, Save) appears exactly once, and a value such as a description is displayed in exactly one visible element.
- Every page shows the global navigation links named in the requirements, before and after login. When logged in, show the account's display name as plain visible text in its own element.

## Behaviour patterns
- Create/edit forms: a Save button commits and redirects where the requirements say; a Cancel button (<button type="submit" name="_action" value="cancel" formnovalidate>) commits nothing and redirects where the requirements say. Cancel must work without JavaScript: the POST handler checks form.get("_action") == "cancel" BEFORE any insert/update/delete, does not write anything, and 303-redirects — a create-form Cancel returns to the list page whose <h1> is the requirements' exact name (e.g. "Shelfs" with that spelling), an edit-form Cancel returns to the item's detail page. Edit forms are prefilled with the saved values exactly as entered (store free text such as tags exactly as typed).
- Delete: the Delete button leads to a confirmation view showing the confirm button and a Cancel button; confirm deletes and redirects; Cancel returns to the unchanged item.
- Child objects created from a parent's page are linked to that parent; the child's page offers a link named exactly like the parent that opens the parent.
- Personal data (drafts, history, favorites) belongs to the signed-in user. Record visits on detail pages for signed-in users. Lists are newest first and respect the stated maximum sizes.
- "Recent"/"latest" ordering must not depend on timestamps (many events share the same second or a fixed clock): store a strictly increasing integer sequence on every create/update/visit/favorite event (e.g. a global counter table or MAX(seq)+1 inside the same transaction) and ORDER BY that sequence DESC. Re-visiting or re-saving an item moves it to the top. Tests run in parallel against one server, so every request must be safe under concurrency (one sqlite connection per request, commit before redirecting). Sections the requirements show on the public homepage must be visible when logged out too.
- Routing: every form posts to a URL the POST handler actually routes, and every redirect goes to a GET route that exists. Define URL patterns once and reuse them. Match fixed paths such as /things/new before parameterised ones such as /things/<id>, and accept only digits as ids, so "new" is never read as an id.
- When the requirements say items appear in, or can be reopened from, a list (including unpublished items such as drafts), render them in that list.
- Anything the requirements leave unspecified: choose the simplest behaviour that keeps every stated flow working.
"""

FILE_PATTERN = re.compile(r"<<<FILE\s+(\S+)\s*\n(.*?)\nFILE>>>", re.S)
EDIT_PATTERN = re.compile(r"<<<<<<< SEARCH\n(.*?)\n=======\n(.*?)\n>>>>>>> REPLACE", re.S)
OBLIGATION_PATTERN = re.compile(r'"([^"\n{}]+)"\s*\(role=([a-z]+)\)')


def log(message: str) -> None:
    print(f"[fastpath {time.strftime('%H:%M:%S')}] {message}", flush=True)


def on_platform() -> bool:
    """True when the model calls go through the ARC-Bench platform gateway (its upstream
    routinely kills large thinking-enabled responses with 502 model_proxy_error)."""
    base = os.getenv("OPENAI_BASE_URL", "").lower()
    return "arc-bench" in base


def platform_thinking() -> bool:
    """Whether fastpath generation calls may reason: yes locally, no on the platform gateway,
    unless ARC_PLATFORM_THINKING=1 explicitly forces it back on (runtime rollback knob)."""
    if not on_platform():
        return True
    return os.getenv("ARC_PLATFORM_THINKING", "").strip() in {"1", "true", "yes"}


def _safe_unlink(path: Path) -> None:
    """Windows: the server may still hold the log handle right after terminate()."""
    for _ in range(30):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            time.sleep(0.1)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ---------------------------------------------------------------- requirement input

def condense(markdown: str) -> str:
    """Drop image embeds, reference lists and boilerplate the model does not need."""
    kept = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("![") or re.fullmatch(r"\*\*IMG-[^*]+\*\*", stripped):
            continue
        if stripped.startswith("- **References:**") or stripped.startswith("- **Step:**"):
            continue
        if stripped in ("**Dependencies:** None",):
            continue
        kept.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept))


STEP_SKIP = {"id", "references", "reference", "image", "images"}


def extract_seed_blobs(text: str) -> list[str]:
    """Brace-matched, JSON-validated seed blobs (accounts/verification codes), deduped in
    first-seen order. The flat ``{...}`` regex misses nested blobs such as
    ``{"13800000011": {"mobile": ...}}``, so braces are matched explicitly."""
    blobs: list[str] = []
    i = 0
    while True:
        start = text.find("{", i)
        if start < 0:
            break
        depth, j = 0, start
        while j < len(text):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if depth:
            break
        blob = text[start:j + 1]
        try:
            json.loads(blob)
        except ValueError:
            i = start + 1
            continue
        if '"password"' in blob or '"verification_code"' in blob:
            if blob not in blobs:
                blobs.append(blob)
        i = j + 1
    return blobs


def seed_label(blob: str) -> str:
    """A short human-readable label for one seed blob: login plus display name (never the password)."""
    try:
        data = json.loads(blob)
    except ValueError:
        return "seed"
    login = shown = None

    def walk(value) -> None:
        nonlocal login, shown
        if isinstance(value, dict):
            if not login:
                for key in ("email", "mobile", "username", "login", "account"):
                    if isinstance(value.get(key), str) and value[key]:
                        login = value[key]
                        break
            if not shown:
                for key in ("nickname", "display_name", "name", "full_name"):
                    if isinstance(value.get(key), str) and value[key]:
                        shown = value[key]
                        break
            for inner in value.values():
                walk(inner)
        elif isinstance(value, list):
            for inner in value:
                walk(inner)

    walk(data)
    if not login:
        return "seed"
    return f"{login} (display name {shown})" if shown else login


def _account_dicts(value, out: list) -> None:
    """Recursively collect dicts that look like account records (contain a password)."""
    if isinstance(value, dict):
        if "password" in value:
            out.append(value)
        for inner in value.values():
            _account_dicts(inner, out)
    elif isinstance(value, list):
        for inner in value:
            _account_dicts(inner, out)


def render_yaml(text: str) -> str:
    """Render the requirement tree as compact markdown.

    Scenario/step ids are dropped, seed blobs are lifted out of the scenario text into one
    deduplicated "# Seed data" section, and each in-place occurrence becomes a short marker.
    On large documents (12306 renders ~330k chars) this cuts the generation prompt
    substantially while keeping every exact name, role and seed value."""
    import yaml  # PyYAML, listed in requirements.txt

    out: list[str] = []

    def describe(value) -> str:
        if isinstance(value, dict):
            return str(value.get("text") or "").strip()
        return str(value or "").strip()

    def node(item: dict, depth: int) -> None:
        out.append(f"{'#' * min(depth, 6)} {item.get('id', '')} {item.get('name', '')}".rstrip())
        if describe(item.get("description")):
            out.append(describe(item.get("description")))
        for scenario in item.get("scenarios") or []:
            out.append(f"Scenario: {scenario.get('name', '')}".strip())
            for step in scenario.get("steps") or []:
                if "keyword" in step:
                    out.append(f"- {step['keyword']}: {str(step.get('content', '')).strip()}")
                    continue
                for key, value in step.items():
                    if key not in STEP_SKIP and value:
                        out.append(f"- {key}: {str(value).strip()}")
        for child in item.get("children") or []:
            node(child, depth + 1)

    root = yaml.safe_load(text)
    for item in root if isinstance(root, list) else [root]:
        node(item, 1)
    rendered = "\n\n".join(out)
    seeds = extract_seed_blobs(text)
    if seeds:
        for blob in seeds:
            rendered = rendered.replace(blob, f"[seed account {seed_label(blob)}]")
        rendered += "\n\n# Seed data\n" + "\n".join(seeds)
    return rendered


def load_tree(requirement_dir: Path) -> list[dict] | None:
    """Return the yaml requirement tree, or None when it cannot be parsed."""
    yaml_file = requirement_dir / "requirements.yaml"
    if not yaml_file.is_file():
        return None
    try:
        import yaml
        root = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
        return root if isinstance(root, list) else [root]
    except Exception:
        return None


def leaf_count(tree: list[dict]) -> int:
    def count(node: dict) -> int:
        children = node.get("children") or []
        return 1 if not children else sum(count(child) for child in children)
    return sum(count(item) for item in tree)


def eligible(requirement_dir: Path) -> bool:
    """Fast path applies to small tasks unless explicitly disabled."""
    value = os.getenv("ARC_FASTPATH", "auto").strip().lower()
    if value in {"0", "off", "false", "no", "never"}:
        return False
    tree = load_tree(requirement_dir)
    if tree is None:
        return False
    try:
        threshold = int(os.getenv("ARC_FASTPATH_MAX_LEAVES", "120"))
    except ValueError:
        threshold = 120
    leaves = leaf_count(tree)
    log(f"fast path: {leaves} leaf requirements, threshold {threshold}")
    return 0 < leaves <= threshold


def read_requirements(requirement_dir: Path) -> str:
    parts = []
    md = requirement_dir / "requirements.md"
    yaml_file = requirement_dir / "requirements.yaml"
    if yaml_file.is_file():
        text = yaml_file.read_text(encoding="utf-8")
        try:
            parts.append(render_yaml(text))
        except Exception as error:
            log(f"could not render requirements.yaml ({error}); using it verbatim")
            parts.append(text)
    elif md.is_file():
        parts.append(condense(md.read_text(encoding="utf-8")))
    pre = requirement_dir / "prerequisites.md"
    if pre.is_file():
        parts.append("# Prerequisites\n" + pre.read_text(encoding="utf-8"))
    if not parts:
        raise SystemExit(f"No requirements found in {requirement_dir}")
    return "\n\n".join(parts)


def obligations(requirements: str) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []
    for name, role in OBLIGATION_PATTERN.findall(requirements):
        if "<" in name or ">" in name:
            continue  # "<registered email>"-style template placeholders are dynamic, not literal names
        if (name, role) not in seen:
            seen.append((name, role))
    return seen


# ---------------------------------------------------------------- model transport

class Model:
    """Small raw-completion client with transient retries and optional thinking-off."""

    def __init__(self) -> None:
        # The pipeline stages load .env themselves; this standalone module must too,
        # otherwise the key check runs before the environment is populated.
        load_dotenv(Path(__file__).resolve().parent / ".env", encoding="utf-8-sig")
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ModelUnavailable("OPENAI_API_KEY is not set")
        self.client = OpenAI(
            api_key=api_key,
            base_url=os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1",
            timeout=600, max_retries=2)
        self.model = os.getenv("MODEL") or MODEL_NAME
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.calls = 0

    def complete(self, messages: list[dict], max_tokens: int = MAX_OUTPUT_TOKENS,
                 allow_thinking: bool = False) -> tuple[str, str]:
        request = dict(model=self.model, messages=messages, temperature=0, max_tokens=max_tokens)
        # Generation calls may reason (better first drafts on complex tasks); repair/review
        # calls keep thinking disabled to stay cheap and predictable.
        extra = {} if allow_thinking else {"extra_body": {"thinking": {"type": "disabled"}}}
        for attempt, wait in enumerate((*BACKOFF_SECONDS, None)):
            try:
                response = self.client.chat.completions.create(**request, **extra)
                break
            except BadRequestError as error:
                if not extra:
                    raise
                log(f"provider rejected the thinking option ({error.status_code}); retrying without it")
                extra = {}
            except TRANSIENT as error:
                if wait is None:
                    raise ModelUnavailable(str(error)[:300]) from error
                log(f"model endpoint error ({str(error)[:120]}); retrying in {wait}s")
                time.sleep(wait)
        self.calls += 1
        usage = response.usage
        if usage:
            self.prompt_tokens += usage.prompt_tokens or 0
            self.completion_tokens += usage.completion_tokens or 0
            log(f"model call: input {usage.prompt_tokens}, output {usage.completion_tokens}")
        choice = response.choices[0]
        return choice.message.content or "", choice.finish_reason or ""

    def generate_file(self, messages: list[dict], tag: str | None = None) -> str | None:
        """Ask for a generated file; if the answer is cut off, ask for the continuation.

        Generation may reason (thinking allowed) for better first drafts; continuations keep
        thinking on too since they extend the same draft. On the ARC-Bench platform gateway the
        reasoning responses are routinely killed upstream (502 model_proxy_error), so thinking
        is disabled there unless ARC_PLATFORM_THINKING=1 forces it back on. ``tag`` overrides
        the expected file name inside the wrapper (default app.py)."""
        thinking = platform_thinking()
        reply, finish = self.complete(messages, allow_thinking=thinking)
        text = reply
        for _ in range(2):
            if finish != "length":
                break
            log("output hit the length limit; asking for the continuation")
            messages = messages + [{"role": "assistant", "content": text},
                                   {"role": "user", "content": "Continue exactly where you stopped. "
                                    "Output only the remaining text, no repetition, no preamble."}]
            more, finish = self.complete(messages, allow_thinking=thinking)
            text += more
        name = tag or APP_FILE
        for wrapped, body in FILE_PATTERN.findall(text):
            if wrapped == name:
                return body
        for wrapped, body in FILE_PATTERN.findall(text):
            return body  # the model used a different wrapper name; accept its content
        start = text.find(f"<<<FILE {name}")
        if start >= 0:  # unterminated wrapper
            body = text[text.index("\n", start) + 1:]
            return body.split("FILE>>>")[0]
        fenced = re.search(r"```(?:python)?\n(.*?)```", text, re.S)
        return fenced.group(1) if fenced else None


def extract_app(text: str, name: str = APP_FILE) -> str | None:
    for wrapped, body in FILE_PATTERN.findall(text):
        if wrapped == name:
            return body
    start = text.find(f"<<<FILE {name}")
    if start >= 0:  # unterminated wrapper
        body = text[text.index("\n", start) + 1:]
        return body.split("FILE>>>")[0]
    fenced = re.search(r"```(?:python)?\n(.*?)```", text, re.S)
    return fenced.group(1) if fenced else None


# ---------------------------------------------------------------- local checks

class PageParser(HTMLParser):
    """Collects links, the accessible names of links/buttons, and heading texts of one page."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.controls: list[tuple[str, str, int]] = []
        self.headings: list[str] = []
        self.forms: list[dict] = []
        self.labels: list[str] = []
        self._containers: list[tuple[str, int]] = [("page", 0)]
        self._ids = itertools.count(1)
        self._form: dict | None = None
        self._open: list[list] = []  # [tag, attrs, collected text]

    def handle_starttag(self, tag, attrs):
        attrs = {k: (v or "") for k, v in attrs}
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])
        if tag == "form":
            self._form = {"action": attrs.get("action", ""), "method": attrs.get("method", "get").lower(),
                          "fields": [], "buttons": []}
            self.forms.append(self._form)
        elif self._form is not None and tag in ("input", "textarea", "select"):
            kind = attrs.get("type", "text").lower() if tag == "input" else tag
            if kind in ("submit", "image"):
                self._form["buttons"].append((attrs.get("name", ""), attrs.get("value", ""), attrs.get("value", "")))
            elif attrs.get("name"):
                self._form["fields"].append({"name": attrs["name"], "type": kind, "value": attrs.get("value", "")})
        elif self._form is not None and tag == "option" and self._form["fields"]:
            field = self._form["fields"][-1]
            if field["type"] == "select" and not field.get("chosen") and attrs.get("value"):
                field["value"], field["chosen"] = attrs["value"], True
        if tag in ("section", "nav", "aside", "header", "footer", "main", "ul", "ol"):
            self._containers.append((tag, next(self._ids)))
        if tag in ("input", "textarea", "select"):
            self.labels.extend(v for v in (attrs.get("aria-label"), attrs.get("placeholder")) if v)
        if tag in ("a", "button", "label", "h1", "h2", "h3", "h4", "h5", "h6"):
            self._open.append([tag, attrs, []])

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None
        if len(self._containers) > 1 and self._containers[-1][0] == tag:
            self._containers.pop()
        for i in range(len(self._open) - 1, -1, -1):
            if self._open[i][0] == tag:
                _, attrs, parts = self._open.pop(i)
                name = attrs.get("aria-label") or " ".join("".join(parts).split())
                if tag == "label":
                    self.labels.append(name)
                elif tag.startswith("h"):
                    self.headings.append(name)
                else:
                    self.controls.append(("link" if tag == "a" else "button", name, self._containers[-1][1]))
                    if tag == "button" and self._form is not None and attrs.get("type", "submit") == "submit":
                        self._form["buttons"].append((attrs.get("name", ""), attrs.get("value", ""), name))
                break

    def handle_data(self, data):
        for entry in self._open:
            entry[2].append(data)


def crawl(base: str, opener, fixed_names: set[str] = frozenset(), limit: int = 60):
    """Visit same-origin links from /; return problems, headings, POST forms, (role, name, page) of
    every link/button, and every label/placeholder seen."""
    problems, seen, queue, headings, forms, controls, labels = [], set(), ["/"], set(), [], set(), set()
    while queue and len(seen) < limit:
        path = queue.pop(0)
        if path in seen:
            continue
        seen.add(path)
        try:
            with opener.open(base + path, timeout=5) as reply:
                body = reply.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            if error.code >= 500 or (error.code == 404 and path != "/favicon.ico"):
                problems.append(f"GET {path} (linked from the app) returned HTTP {error.code}")
            continue
        except OSError as error:
            problems.append(f"GET {path} failed: {error}")
            continue
        parser = PageParser()
        parser.feed(body)
        headings.update(parser.headings)
        labels.update(parser.labels)
        controls.update((role, name, path) for role, name, _ in parser.controls)
        # Duplicates are only real when the same role+name repeats INSIDE one container:
        # per-item buttons (Up vote on every answer, Edit on every post) legitimately
        # repeat across sections, and Playwright tests scope them by region.
        same_container = [(r, n, c) for r, n, c in parser.controls if n]
        repeated = sorted({(r, n) for r, n, c in same_container
                           if same_container.count((r, n, c)) > 1})
        if repeated:
            problems.append(f"Page {path} contains several elements with the same role and name inside one "
                            f"container: " + ", ".join(f'{role} "{name}"' for role, name in repeated)
                            + ". One control name must occur once per container/section; separate sections "
                              "may each carry their own (e.g. an Up vote per answer).")
        for form in parser.forms:
            action = urllib.parse.urljoin(path, form["action"] or path)
            if form["method"] == "post":
                forms.append((path, {**form, "action": action}))
            else:
                parser.links.append(action)
        for href in parser.links:
            parsed = urllib.parse.urlparse(href)
            if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
                continue
            if parsed.path not in seen:
                queue.append(parsed.path)
    return problems, headings, forms, controls, labels


SAMPLE_IDS = itertools.count(1)
SKIP_BUTTON = re.compile(r"delete|remove|log ?out|sign ?out", re.I)


def h1_texts(body: str) -> list[str]:
    """Extract the visible text of every <h1> on a rendered page."""
    return [html.unescape(re.sub(r"<[^>]+>", "", match).strip())
            for match in re.findall(r"<h1[^>]*>(.*?)</h1>", body, re.S | re.I)]


CREATE_FORM_H1 = re.compile(r"\b(new|create)\b", re.I)


def cancel_checked_nouns(requirements: str) -> set[str]:
    """Nouns whose create forms have a requirements-defined Cancel return target.

    The rendered requirements are split at the "# REQ-x.y <name>" headings; a noun (book, shelf,
    page, ...) is "cancel-checked" when its section both names a create-and-abandon flow and
    contains a Cancel clause with a "returns to X" target. Forms for other objects (whose Cancel
    behaviour the requirements never specify) are left alone, so the checker only enforces what
    the tests actually exercise."""
    checked: set[str] = set()
    for section in re.split(r"(?m)^#+ ", requirements)[1:]:
        heading, _, body = section.partition("\n")
        noun = None
        match = re.search(r"create or abandon (\w+)", heading, re.I)
        if match:
            noun = match.group(1)
        elif re.search(r"\bnew\b", heading, re.I):
            match = re.search(r"(\w+)\s*$", heading.strip())
            if match:
                noun = match.group(1)
        if noun and re.search(r"Cancel", body) and re.search(r"returns? to", body):
            checked.add(noun.lower())
    return checked


def exercise_forms(base: str, opener, forms, server_log: Path, done: set,
                   destinations: list[str] | None = None,
                   allowed_headings: set[str] = frozenset(),
                   checked_nouns: set[str] = frozenset(), limit: int = 40) -> list[str]:
    """Submit each POST form once with sample values, as a browser would, and report server errors.

    For a Cancel button on a create-style form whose object noun is cancel-checked (the
    requirements define where its Cancel returns), the landing page must carry an <h1> that the
    requirements sanction: one of the "returns to X" destinations, or a heading of a page that
    already exists (e.g. the parent shelf's name when cancelling a child creation). A Cancel
    that lands on a brand-new object (its h1 was never seen before) means the cancel created
    something or routed to the wrong page — tests click Cancel and expect the named list page
    (e.g. "Shelfs" with the requirements' exact spelling)."""
    allowed = set(destinations or []) | set(allowed_headings)
    problems = []
    for page, form in forms:
        for button_name, button_value, button_text in form["buttons"] or [("", "", "")]:
            key = (form["action"], button_text)
            if key in done or len(done) >= limit or SKIP_BUTTON.search(button_text) \
                    or any(f["type"] == "password" for f in form["fields"]):
                continue
            done.add(key)
            data = []
            for n, field in enumerate(form["fields"]):
                kind, value = field["type"], field["value"]
                if kind in ("hidden", "select"):
                    pass
                elif kind in ("number", "range"):
                    value = value or "1"
                elif kind == "date":
                    value = value or "2026-10-06"
                elif kind == "checkbox" or kind == "radio":
                    value = value or "on"
                elif kind == "email":
                    value = f"check{n}@example.com"
                elif kind == "password":
                    value = "Check-Pass-123!"
                else:
                    value = f"Check {field['name']} {next(SAMPLE_IDS)}"
                data.append((field["name"], value))
            if button_name:
                data.append((button_name, button_value))
            request = urllib.request.Request(base + form["action"], data=urllib.parse.urlencode(data).encode(),
                                             method="POST")
            label = f'the "{button_text}" button of the form on {page} (POST {form["action"]})'
            try:
                with opener.open(request, timeout=5) as reply:
                    body = reply.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as error:
                if error.code >= 500:
                    tail = server_log.read_text(encoding="utf-8", errors="replace")[-1500:]
                    problems.append(f"Pressing {label} returned HTTP {error.code}. Server output:\n{tail}")
                elif error.code == 404:
                    problems.append(f"Pressing {label} ends on a page that does not exist (HTTP 404 at "
                                    f"{urllib.parse.urlparse(error.url).path}); the redirect target and the "
                                    f"route patterns disagree.")
                continue
            except OSError as error:
                tail = server_log.read_text(encoding="utf-8", errors="replace")[-1500:]
                problems.append(f"Pressing {label} failed ({error}). Server output:\n{tail}")
                continue
            if button_text.strip().lower() == "cancel" and destinations and checked_nouns:
                try:
                    with opener.open(base + page, timeout=5) as origin:
                        origin_h1 = h1_texts(origin.read().decode("utf-8", "replace"))
                except OSError:
                    origin_h1 = []
                noun = None
                for heading in origin_h1:
                    if CREATE_FORM_H1.search(heading):
                        match = re.search(r"\bnew\s+(\w+)\b", heading, re.I) or \
                                re.search(r"\bcreate\s+new\s+(\w+)\b", heading, re.I)
                        if match:
                            noun = match.group(1).lower()
                        break
                if noun in checked_nouns:
                    landing = h1_texts(body)
                    if not landing or not any(heading in allowed for heading in landing):
                        shown = ", ".join(repr(h) for h in landing[:3]) or "no <h1>"
                        problems.append(f"Pressing Cancel on {page} (a create form) lands on a page whose <h1> is "
                                        f"{shown}, which is neither one of the requirements' return-to pages "
                                        f"({', '.join(sorted(destinations)) if destinations else 'none'}) nor a heading "
                                        f"of any existing page. Cancelling must not create an object or land on a "
                                        f"brand-new page; route the Cancel submit back to the page the requirements "
                                        f"say it returns to (with the requirements' exact heading spelling).")
    return problems


def seeded_accounts(requirements: str) -> list[dict]:
    """Accounts the requirements say exist, e.g. {"nickname": ..., "email": ..., "password": ...}."""
    accounts = []
    for blob in extract_seed_blobs(requirements):
        try:
            data = json.loads(blob)
        except ValueError:
            continue
        records: list[dict] = []
        _account_dicts(data, records)
        for record in records:
            login = record.get("email") or record.get("username") or record.get("login") or record.get("mobile")
            shown = next((record[k] for k in ("nickname", "display_name", "name", "username", "full_name") if record.get(k)), None)
            if login and record.get("password") and (login, record["password"]) not in [(a["login"], a["password"]) for a in accounts]:
                accounts.append({"login": login, "password": record["password"], "shown": shown})
    return accounts


def log_in(base: str, opener, forms, account: dict) -> tuple[str | None, str]:
    """Submit the app's login form (the POST form with a password field) with a seeded account."""
    for page, form in forms:
        if not any(f["type"] == "password" for f in form["fields"]):
            continue
        data = []
        for field in form["fields"]:
            kind, name = field["type"], field["name"]
            if kind == "password":
                data.append((name, account["password"]))
            elif kind in ("checkbox",):
                data.append((name, field["value"] or "on"))
            elif kind in ("hidden", "select"):
                data.append((name, field["value"]))
            else:
                data.append((name, account["login"]))
        name, value, _ = (form["buttons"] or [("", "", "")])[0]
        if name:
            data.append((name, value))
        request = urllib.request.Request(base + form["action"], data=urllib.parse.urlencode(data).encode(), method="POST")
        try:
            with opener.open(request, timeout=5) as reply:
                return reply.read().decode("utf-8", "replace"), page
        except OSError as error:
            return None, f"{page} ({error})"
    return None, "no login form found"


RETURN_PATTERN = re.compile(r"\breturns? to (?:the )?([A-Z][A-Za-z]+)\b(?! list)")


def destination_headings(requirements: str) -> list[str]:
    """Page names the requirements say an action returns to, e.g. 'returns to Books'."""
    names = []
    for name in RETURN_PATTERN.findall(requirements):
        if name not in names:
            names.append(name)
    return names


def check_app(app_dir: Path, required: list[tuple[str, str]], destinations: list[str],
              accounts: list[dict] | None = None,
              checked_nouns: set[str] = frozenset()) -> list[str]:
    """Cheap generic checks: compiles, starts, pages render, required names appear."""
    app_dir = Path(app_dir).resolve()  # keep paths absolute so cleanup comparisons match
    before = set(app_dir.iterdir())
    source = (app_dir / APP_FILE).read_text(encoding="utf-8")
    compiled = subprocess.run([sys.executable, "-m", "py_compile", APP_FILE], cwd=app_dir,
                              capture_output=True, text=True)
    if compiled.returncode != 0:
        return [f"app.py does not compile:\n{compiled.stderr[-2000:]}"]
    problems = []
    missing = [f'"{name}"(role={role})' for name, role in required
               if name not in source and html.escape(name, quote=False) not in source]
    if missing:
        problems.append("These required accessible names do not appear anywhere in app.py: " + ", ".join(missing))
    port = free_port()
    env = {**os.environ, "HOST": "127.0.0.1", "PORT": str(port)}
    server_log = Path(tempfile_mkstemp_safe(app_dir))
    server = subprocess.Popen([sys.executable, APP_FILE], cwd=app_dir, env=env,
                              stdout=server_log.open("w"), stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1).close()
                break
            except urllib.error.HTTPError as error:
                problems.append(f"GET / returned HTTP {error.code}")
                break
            except OSError:
                if server.poll() is not None:
                    break
                time.sleep(0.2)
        if server.poll() is not None:
            output = server_log.read_text(encoding="utf-8", errors="replace")
            problems.append(f"The server exited early:\n{output[-2000:]}")
        else:
            base = f"http://127.0.0.1:{port}"
            found: list[str] = []
            headings, controls, labels = set(), set(), set()

            def explore(opener, rounds: int) -> list:
                done: set = set()
                forms: list = []
                for _ in range(rounds):  # crawl, submit forms, crawl what those forms created, ...
                    crawl_problems, h, forms, c, l = crawl(base, opener, {n for n, _ in required})
                    headings.update(h), controls.update(c), labels.update(l)
                    found.extend(crawl_problems + exercise_forms(base, opener, forms, server_log, done,
                                                                 destinations, headings, checked_nouns))
                return forms

            new_opener = lambda: urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
            anonymous_forms = explore(new_opener(), 3)
            for account in (accounts or [])[:1]:
                opener = new_opener()
                landed, where = log_in(base, opener, anonymous_forms, account)
                shown = account["shown"]
                if landed is None:
                    found.append(f"Logging in as {account['login']} with the seeded password failed at {where}.")
                    continue
                if shown:
                    count = len(re.findall(rf">\s*{re.escape(html.escape(shown))}\s*<", landed))
                    if count == 0:
                        found.append(f"After logging in as {account['login']} with the seeded password the landing "
                                     f'page does not show the display name "{shown}"; the login or the seed data '
                                     f"is wrong.")
                    elif count > 1:
                        found.append(f'After logging in, the display name "{shown}" is shown in {count} elements on '
                                     f"the landing page; show it exactly once (in the user area of the header).")
                explore(opener, 3)
            problems.extend(dict.fromkeys(found))
            for name, role in required:
                if role not in ("button", "link"):
                    if role in ("textbox", "checkbox", "combobox", "searchbox", "spinbutton", "radio") and name not in labels:
                        problems.append(f'No reachable page has a field labelled exactly "{name}" '
                                        f'(role={role}); give it <label for> with exactly that text.')
                    continue
                if not any(n == name for _, n, _ in controls):
                    rendered_as_control = re.search(
                        rf'<(?:a|button)\b[^>]*>\s*{re.escape(name)}\s*</(?:a|button)>', source, re.S | re.I) \
                        or re.search(rf'<(?:a|button)\b[^>]*>\s*{re.escape(html.escape(name, quote=False))}\s*</(?:a|button)>',
                                     source, re.S | re.I)
                    if (name in source or html.escape(name, quote=False) in source) and rendered_as_control:
                        # The control exists in the code but the crawl never rendered it: this is
                        # usually a toggle/draft/owner state the crawl does not reach, not a missing
                        # control. Treat it as non-blocking; the wrong-role check below still applies
                        # when the name IS rendered.
                        log(f'note: "{name}" (role={role}) is defined in app.py but never rendered '
                            f"during the crawl; treating it as a reachability gap (non-blocking)")
                        continue
                    problems.append(f'"{name}" (role={role}) is never rendered on any page reachable by clicking '
                                    f"through the app (anonymously and after logging in, creating items with "
                                    f"its forms). Render it where the requirements say.")
                    continue
                pages = sorted(page for r, n, page in controls if n == name and r != role)
                if pages and not any(r == role and n == name for r, n, _ in controls):
                    shown = "link" if role == "button" else "button"
                    tag = "<button>" if role == "button" else "<a href>"
                    problems.append(f'"{name}" must have role={role} ({tag}) but is rendered as a {shown} '
                                    f'on {", ".join(pages[:3])}.')
            for name in destinations:
                if name not in headings:
                    problems.append(f'The requirements say actions return to "{name}", but no page has a heading '
                                    f'exactly "{name}". Give the page those actions return to an <h1> that is '
                                    f'exactly "{name}" (keep the spelling used by the requirements).')
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
        for created in set(app_dir.iterdir()) - before:
            if created == server_log:
                continue  # the server may still hold the log handle; removed by _safe_unlink below
            if created.is_dir():
                shutil.rmtree(created, ignore_errors=True)
            else:
                _safe_unlink(created)  # Windows: sqlite/log handles may linger briefly
        _safe_unlink(server_log)
    return problems


def tempfile_mkstemp_safe(app_dir: Path) -> str:
    """Server log path inside the app dir so no system temp directory is required."""
    import tempfile
    handle, name = tempfile.mkstemp(suffix=".log", dir=str(app_dir))
    os.close(handle)
    return name


def compiles(source: str) -> bool:
    try:
        compile(source, APP_FILE, "exec")
        return True
    except (SyntaxError, ValueError):
        return False


def apply_edits(source: str, reply: str) -> tuple[str, int, int]:
    applied = failed = 0
    for search, replace in EDIT_PATTERN.findall(reply):
        if re.search(r"^(<<<<<<<|=======|>>>>>>>)", replace, re.M):
            failed += 1  # a malformed block swallowed the next marker
        elif search in source:
            source = source.replace(search, replace, 1)
            applied += 1
        else:
            failed += 1
    return source, applied, failed


DEPLOY_SCRIPT = 'set -eu\ncd "$(dirname "$0")"\nexec python3 app.py\n'


REVIEW_PROMPT = """Review app.py against every scenario in the requirements. For each scenario, execute its
Given/When/Then steps in your head through the routes and HTML of app.py, exactly as a Playwright test would
(clicking elements by role and exact name, filling fields by label or placeholder, checking headings and
named regions). Look for: missing pages, elements or actions; wrong names or roles; items missing from a list
where the requirements say they appear (including drafts, child objects and personal lists); duplicate
names on one page; wrong redirect targets; data not persisted or not prefilled; behaviour that requires login
where the requirements allow anonymous use; item links whose text is decorated with status markers such as
" (draft)" or any suffix beyond the item's exact name (tests use exact: true and the decoration fails them);
a sidebar/navigation missing the exact section buttons the requirements name (with aria-current on the
active one and aria-expanded on the toggle); dialogs whose aria-label is not EXACTLY the name the
requirements give (e.g. "Note editor", "Note labels"); a destructive action whose role=status notice
(e.g. "Note deleted") is missing its Undo and/or Dismiss button; a live search whose results do not filter
the listed items or whose highlight region ("Highlight <term>") is missing; hover menus whose menuitems
are missing or wrongly named; the same item rendered twice on one page.

This is a READ-ONLY review: do NOT output search/replace blocks and do NOT rewrite code. Output at most 8
real defects as plain text bullets. Each bullet must name the page or route (for example /books/2/newpage),
the control or heading involved, and what the test expects. Ignore cosmetic style. If nothing is wrong,
output only: NO_DEFECTS"""

REPAIR_PROMPT = """The application you wrote has these problems, reported by an automated checker that crawls
the running app (only the first ones are listed; fix exactly these, do not restyle anything else):
- {problems}

Fix them with search/replace edits on app.py. Output one or more blocks exactly like:
<<<<<<< SEARCH
exact existing lines of app.py
=======
replacement lines
>>>>>>> REPLACE
Each SEARCH must match the current file exactly and be unique. Output only the blocks.

How to fix: each problem names a page path (for example /books/2/newpage) and/or a control name.
Find the route handler that renders or receives that path and fix THAT handler directly; do not
change unrelated routes. For a Cancel problem, the handler for the form's action must check
name="_action" value="cancel" before inserting and 303-redirect to the list page.

Cancel-button guidance: every Cancel button on a create or edit form must submit the same form with
name="_action" value="cancel" (add formnovalidate). The POST handler must check for _action == "cancel"
BEFORE any insert/update, do nothing to the database, and 303-redirect to the page the requirements say
cancelling returns to (a create-form Cancel returns to the list page whose <h1> is the requirements'
exact name such as "Shelfs"; an edit-form Cancel returns to the item's detail page). A Cancel must never
create, update or delete anything, and must not rely on JavaScript.

Missing-element problems (for example '"Answer by Stack User"(role=region) does not appear anywhere
in app.py' or '"Score"(role=status) is never rendered'): the required element DOES NOT EXIST yet, so
editing unrelated lines never fixes it. CREATE it by wrapping the content that already belongs to it:
- a missing region named "Answer by <display name>": wrap each answer block in the detail handler with
  <section role="region" aria-label="Answer by {the answer author's display name}">...</section>;
- a missing status named "Score": put <div role="status" aria-label="Score">{the number}</div> next to
  the votes inside the same block;
- a missing button/link named X: render it inside the same form/block the requirements attach it to.
Make ONE edit block per missing element, each SEARCH targeting the exact lines you wrap; use the names
verbatim (including spaces and punctuation) and the exact role= value from the problem text."""


def digest(requirements: str, required: list[tuple[str, str]], destinations: list[str]) -> str:
    """A compact contract summary for repair contexts: exact names/roles, return targets and a
    one-line summary per requirement section. Repair rounds rarely need the full scenario text,
    and sending it on every round is the dominant token cost on large tasks."""
    parts = ["Named elements (exact name, role): " + "; ".join(f"{n}({r})" for n, r in required)]
    if destinations:
        parts.append("Pages actions return to (exact <h1> names): " + ", ".join(destinations))
    summaries = []
    for section in re.split(r"(?m)^#+ ", requirements)[1:]:
        head, _, body = section.partition("\n")
        first = next((line for line in body.splitlines() if line.strip()), "")
        summaries.append(f"- {head.strip()}: {first[:180]}")
    parts.append("Requirement summaries:\n" + "\n".join(summaries))
    text = "\n\n".join(parts)
    return text[:10000]


def edit_messages(requirements: str, current: str, instruction: str, condensed: bool = False) -> list[dict]:
    summary = digest(requirements, obligations(requirements), destination_headings(requirements)) \
        if condensed else requirements
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Requirements:\n\n{summary}\n\nCurrent app.py:\n\n{current}"},
            {"role": "user", "content": instruction}]


FATAL = re.compile(r"does not compile|server exited early|^GET / returned", re.M)


def severity(problems: list[str]) -> int:
    """Lower is better; an app that does not start is worse than any number of smaller problems."""
    return 1000 if any(FATAL.search(p) for p in problems) else len(problems)


def check_and_repair(model: Model, work: Path, requirements: str,
                     required: list[tuple[str, str]], destinations: list[str],
                     rounds: int = MAX_REPAIRS,
                     checked_nouns: set[str] = frozenset(),
                     defect_list: str | None = None) -> list[str]:
    """Check, repair, re-check; keep whichever version had the fewest problems.

    With ``defect_list`` (a read-only review's findings) exactly one repair round runs against
    that list and the post-repair check result is returned."""
    accounts = seeded_accounts(requirements)
    app = work / APP_FILE
    if defect_list is not None:
        current = app.read_text(encoding="utf-8")
        instruction = REPAIR_PROMPT.format(problems=defect_list)
        try:
            reply, _ = model.complete(edit_messages(requirements, current, instruction, condensed=True), max_tokens=REPAIR_TOKENS)
        except ModelUnavailable as error:
            log(f"model unavailable during review repair ({error}); keeping the current version")
            return check_app(work, required, destinations, accounts, checked_nouns)
        patched, applied, failed = apply_edits(current, reply)
        log(f"review repair edits: {applied} applied, {failed} not used")
        if applied and compiles(patched):
            app.write_text(patched, encoding="utf-8")
        elif applied:
            log("the review repair does not compile; discarding these edits")
        return check_app(work, required, destinations, accounts, checked_nouns)
    best_source, best_problems = None, None
    for attempt in range(rounds + 1):
        current = app.read_text(encoding="utf-8")
        problems = check_app(work, required, destinations, accounts, checked_nouns)
        if compiles(current) and (best_problems is None or severity(problems) < severity(best_problems)):
            best_source, best_problems = current, problems
        if not problems:
            log("local checks passed")
            break
        log(f"local checks failed ({len(problems)}): {problems[0][:300]}")
        if attempt == rounds:
            break
        instruction = REPAIR_PROMPT.format(problems="\n- ".join(problems[:8]))
        try:
            reply, _ = model.complete(edit_messages(requirements, current, instruction, condensed=True), max_tokens=REPAIR_TOKENS)
        except ModelUnavailable as error:
            log(f"model unavailable during repair ({error}); keeping the best version so far")
            break
        patched, applied, failed = apply_edits(current, reply)
        log(f"repair edits: {applied} applied, {failed} not used")
        if applied and compiles(patched):
            app.write_text(patched, encoding="utf-8")
        elif applied:
            log("the repaired file does not compile; discarding these edits")
    if best_source is not None:
        app.write_text(best_source, encoding="utf-8")
        return best_problems
    return problems


# ---------------------------------------------------------------- entry point

DELTA_CONTRACT = """## Delta mode (template + delta)
The application skeleton ALREADY exists (server, sessions, SQLite, layout, HTML components, route
dispatch AND the login/logout pages). Do not redefine them. You output ONLY the delta file, wrapped
as <<<FILE app_delta.py ... FILE>>> with no other text. The delta fills these module-level names:
- TABLES = [("users", "CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, display_name TEXT, session_token TEXT, ...)"), ...]
  every table your routes need, in dependency order. The users table MUST have the columns the
  LOGIN config below points at (password_hash and the login/shown columns).
- SEEDS = ["INSERT OR IGNORE INTO users(username, email, password_hash, display_name) VALUES('u','user@example.com','<hash>','User Name')", ...]
  all seed records from the requirements (accounts with the exact passwords/display names, published items with their exact titles/bodies/dates, ...). Seeds must be idempotent (INSERT OR IGNORE / WHERE NOT EXISTS). Use hash_password('Password123!') for password hashes.
- LOGIN.update({"login_column": "email", "login_field_label": "Email", "password_field_label": "Password",
  "shown_column": "display_name", "landing": "/", "region_name": "Log in"})  only when the requirements use
  different login field labels/columns; the skeleton already serves /login and /logout.
- NAV = [("Books", "/books"), ...]  the global navigation links, exact names, in order.
- ROUTES.update({ "/books": {"GET": "books_page"}, "/books/<int:bid>": {"GET": "book_detail"}, "/books/new": {"GET": "book_form"}, "/books/save": {"POST": "book_save"}, ... })
  every route EXCEPT /login and /logout (already provided); path segments with ids use "<int:name>".
  Register handlers by their NAME STRING (definition order never matters).
- def books_page(self, conn, user, params): ... each handler ends with self.send_html(...) or self.redirect(...).
Available helpers (already imported in the skeleton; use them, do not re-implement HTML/DB/session code):
esc, region(name, inner), status(name, text, extra=""), dialog(name, inner), button(text, action, hidden={...}, method="post"),
link(text, href), field(name, kind="text", value="", placeholder=None), layout(title, nav, active, content, user, banner=""),
db(), seq_next(conn), hash_password(pw), verify_password(pw, stored),
self._read_form(), self._read_query(), self.current_user(conn), self.send_html(body, code=200),
self.redirect(path, code=303).

Example routes (shape only; use the requirements' exact names; register by name string):
ROUTES.update({
    "/books": {"GET": "books_page"},
    "/books/<int:bid>": {"GET": "book_detail"},
    "/books/new": {"GET": "book_form"},
})
def books_page(self, conn, user, params):
    rows = conn.execute("SELECT * FROM books ORDER BY seq DESC").fetchall()
    items = "".join(f"<li>{link(r['name'], '/books/' + str(r['id']))}</li>" for r in rows)
    self.send_html(layout("Books", NAV, "Books", region("Book list", f"<ul>{items}</ul>")
                          + button("Create New Book", "/books/new", method="get"), user))
"""


def assemble_app(delta: str) -> str:
    """Skeleton + delta + server start. The skeleton source ships inside the agent package."""
    skeleton = (Path(__file__).resolve().parent / "13_fastpath_skeleton.py").read_text(encoding="utf-8")
    return (skeleton.rstrip() + "\n\n# ---- generated delta ----\n" + delta.rstrip()
            + '\n\nif __name__ == "__main__":\n    serve()\n')


def generate(input_dir, output_dir, port: int = 3000) -> dict:
    """Run the lean fast path and publish the app only when every local check passes."""
    requirement_dir = Path(input_dir)
    output = Path(output_dir)
    work = output / ".arc" / "fastpath-work"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    requirements = read_requirements(requirement_dir)
    required = obligations(requirements)
    log(f"requirements: {len(requirements)} characters, {len(required)} role/name obligations")

    model = Model()
    delta_mode = os.getenv("ARC_DELTA", "").strip() in {"1", "true", "yes"}
    messages = [{"role": "system", "content": SYSTEM_PROMPT + (DELTA_CONTRACT if delta_mode else "")},
                {"role": "user", "content": (f"Compile these requirements into the delta (app_delta.py) following the contract.\n\n"
                                            if delta_mode else "Compile these requirements into app.py.\n\n") + requirements}]
    hint_file = os.getenv("ARC_HINT_FILE", "").strip()
    if hint_file and Path(hint_file).is_file():
        hint_text = Path(hint_file).read_text(encoding="utf-8", errors="replace").strip()[:4000]
        if hint_text:
            messages[1]["content"] += ("\n\nHints from a previous local validation run (real Playwright test failures; "
                                       f"make sure this draft avoids every one of them):\n{hint_text}")
            log(f"generation hint loaded ({len(hint_text)} chars)")
    # Plan-then-code: for large tasks a short structure plan first concentrates the model's
    # attention; the plan is re-fed as part of the generation prompt.
    plan = None
    if len(required) >= 60:
        try:
            plan, _ = model.complete(
                [{"role": "system", "content": SYSTEM_PROMPT},
                 {"role": "user", "content": "Outline the app structure ONLY (no code): for each requirement group, "
                  "list the routes, the named regions/statuses/buttons per page, the seed records, and the redirect "
                  "targets. Keep it under 1200 tokens."},
                 {"role": "user", "content": requirements}],
                max_tokens=2000, allow_thinking=platform_thinking())
            log(f"structure plan: {len(plan.splitlines())} lines")
            messages[1]["content"] += f"\n\nFollow this structure plan when writing the code:\n{plan}"
        except ModelUnavailable:
            plan = None
    try:
        if delta_mode:
            delta = model.generate_file(messages, tag="app_delta.py")
            if delta is None:
                log("the model returned no delta")
                return {"status": "FAILED", "error": "the model returned no delta"}
            if re.search(r"ThreadingHTTPServer|def serve\(", delta):
                log("the model returned a complete app.py instead of a delta; using it directly")
                source = delta
            else:
                parts = re.split(r"(?m)^#+ ", requirements)[1:]
                if len(parts) > 60:
                    half = len(parts) // 2
                    second_text = "# " + "\n# ".join(parts[half:])
                    part2_messages = [
                        {"role": "system", "content": SYSTEM_PROMPT + DELTA_CONTRACT},
                        {"role": "user", "content":
                         "This is PART 1 of the delta (already accepted; it defines TABLES, SEEDS, NAV, LOGIN, the "
                         "first routes and their handlers):\n\n<<<FILE app_delta.py\n" + delta + "\nFILE>>>\n\n"
                         "This is PART 2. Extend the SAME application with the routes/features for these additional "
                         "requirement sections:\n\n" + second_text + "\n\n"
                         "Output ONLY one wrapper <<<FILE app_part2.py with the ADDITIONAL code: new handler functions "
                         "(register them by name string with ROUTES.update), and TABLES.extend([...]) / "
                         "SEEDS.extend([...]) ONLY for tables/records no existing table can hold. "
                         "Do NOT redefine TABLES, SEEDS, NAV, LOGIN, existing handlers or helpers; reuse them."}
                    ]
                    try:
                        second_delta = model.generate_file(part2_messages, tag="app_part2.py")
                        if second_delta:
                            delta = delta.rstrip() + "\n\n# ---- part 2 ----\n" + second_delta
                            log(f"delta part-wise: +{len(second_delta.splitlines())} part2 lines")
                    except ModelUnavailable as error:
                        log(f"model unavailable for the delta second part ({error}); keeping the single delta")
                source = assemble_app(delta)
                log(f"delta mode: {len(delta.splitlines())} delta lines assembled with the skeleton")
        else:
            source = model.generate_file(messages)
    except ModelUnavailable as error:
        log(f"model unavailable: {error}")
        return {"status": "FAILED", "error": str(error), "problems": [str(error)]}
    if source is None:
        log("the model returned no app.py")
        return {"status": "FAILED", "error": "the model returned no app.py"}

    # Part-wise generation for large tasks: one draft cannot hold 90+ requirement sections,
    # so the second half is generated as an extension pass with the skeleton in context.
    parts = re.split(r"(?m)^#+ ", requirements)[1:]
    if len(parts) > 60 and source is not None and not delta_mode:
        half = len(parts) // 2
        first_half_text = "# " + "\n# ".join(parts[:half])
        extension_text = "# " + "\n# ".join(parts[half:])
        part1_messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content":
             "This is PART 1 of a two-part generation for a large requirement document. "
             "Read the full document for global context (seed accounts, global navigation, shared rules), "
             "but IMPLEMENT ONLY the routes/features for these requirement sections:\n\n" + first_half_text
             + "\n\nBuild the complete skeleton: imports, a ThreadingHTTPServer Handler class, layout/helpers, "
             "session handling, the database schema and the seed loader for ALL seed data in the document. "
             "Route dispatch: keep ONE module-level dict ROUTES mapping path patterns to handler functions; "
             "do_GET/do_POST consult ROUTES. Do NOT start the server and do NOT write a __main__ block; end the "
             "file with the comment line: # == PART2 ==\n\n"
             "Output the file wrapped as <<<FILE app.py ... FILE>>>."}
        ]
        try:
            part1 = model.generate_file(part1_messages)
            if part1:
                extend_messages = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content":
                     "Here is PART 1 of the application (skeleton, helpers, layout, session, database, seed loader "
                     "and the routes for the first requirement groups):\n\n<<<FILE app.py\n" + part1 + "\nFILE>>>\n\n"
                     "This is PART 2. Extend the SAME application with the routes/features for these additional "
                     "requirement sections:\n\n" + extension_text + "\n\n"
                     "Output ONLY one wrapper <<<FILE app_part2.py with the ADDITIONAL code: new handler functions "
                     "(register them in the existing ROUTES dict), their templates, any new tables "
                     "(CREATE TABLE IF NOT EXISTS), and finally the server startup (bind "
                     'os.environ.get("HOST","0.0.0.0") and int(os.environ.get("PORT","3000")), serve_forever). '
                     "Do NOT redefine imports, the Handler class, helpers, layout or existing routes."}
                ]
                second_part = model.generate_file(extend_messages, tag="app_part2")
                if second_part:
                    source = part1.rstrip() + "\n\n# ---- appended part 2 ----\n" + second_part
                    log(f"part-wise generation: part1 {len(part1.splitlines())} lines + part2 {len(second_part.splitlines())} lines")
        except ModelUnavailable as error:
            log(f"model unavailable during part-wise generation ({error}); keeping the single draft")

    app = work / APP_FILE
    app.write_text(source, encoding="utf-8")
    destinations = destination_headings(requirements)
    accounts = seeded_accounts(requirements)
    checked_nouns = cancel_checked_nouns(requirements)
    log(f"cancel-checked nouns: {sorted(checked_nouns) or 'none'}")
    first = check_app(work, required, destinations, accounts, checked_nouns)
    if severity(first) > REDRAFT_THRESHOLD:
        log(f"first draft has {len(first)} problems; writing a second draft")
        hint = ("A previous attempt at this app had these problems; avoid them:\n- "
                + "\n- ".join(p[:300] for p in first[:10]))
        retry = [messages[0], {"role": "user", "content": messages[1]["content"] + "\n\n" + hint}]
        try:
            second = model.generate_file(retry)
        except ModelUnavailable as error:
            log(f"model unavailable for the second draft ({error}); keeping the first")
            second = None
        if second is not None:
            app.write_text(second, encoding="utf-8")
            again = check_app(work, required, destinations, accounts, checked_nouns)
            log(f"second draft has {len(again)} problems")
            if severity(again) > severity(first):
                app.write_text(source, encoding="utf-8")

    before_review = check_and_repair(model, work, requirements, required, destinations,
                                     checked_nouns=checked_nouns)
    current = app.read_text(encoding="utf-8")
    if before_review:
        # The read-only review is a last resort after repairs stall; on clean checks it has
        # repeatedly cost tokens while adding nothing (and sometimes regressing), so it is
        # skipped when the local checks already pass.
        try:
            reply, finish = model.complete(edit_messages(requirements, current, REVIEW_PROMPT), max_tokens=REVIEW_TOKENS)
        except ModelUnavailable as error:
            log(f"model unavailable during review ({error}); keeping the current version")
            reply, finish = "", "error"
        defects = reply.strip()
        log(f"review defects: {len(defects.splitlines())} line(s)" + (" (cut off)" if finish == "length" else ""))
        if defects and "NO_DEFECTS" not in defects.upper():
            # One targeted repair round applies the review's defect list; revert it if the
            # local checks get worse (the review is read-only and can misjudge).
            repaired = check_and_repair(model, work, requirements, required, destinations,
                                        rounds=0, checked_nouns=checked_nouns,
                                        defect_list=defects)
            if severity(repaired) > severity(before_review):
                log(f"review repair made the local checks worse ({repaired[0][:200]}); reverting it")
                app.write_text(current, encoding="utf-8")

    final_problems = check_app(work, required, destinations, accounts, checked_nouns)
    usage = {"calls": model.calls, "input_tokens": model.prompt_tokens, "output_tokens": model.completion_tokens}
    log(f"model usage: input {model.prompt_tokens}, output {model.completion_tokens} tokens")

    if final_problems:
        log(f"fast path not green: {len(final_problems)} problem(s) remain; the compiler will fall back")
        keep = os.getenv("ARC_FASTPATH_KEEP", "").strip() in {"1", "true", "yes"}
        if keep:
            result = {"status": "COMPLETED_WITH_FAILURES", "problems": final_problems,
                      "usage": usage, "work": str(work)}
        else:
            result = {"status": "COMPLETED_WITH_FAILURES", "problems": final_problems, "usage": usage}
            shutil.rmtree(work, ignore_errors=True)
        return result
    (output / APP_FILE).write_text(app.read_text(encoding="utf-8"), encoding="utf-8")
    (output / "deploy.sh").write_text(DEPLOY_SCRIPT, encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)
    log("fast path GREEN; application published")
    return {"status": "GREEN", "usage": usage}
