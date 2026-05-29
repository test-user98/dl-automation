"""
Learning store — the agent's evolving memory of how to handle edge cases.

When the agent gets stuck and a human helps, the solution is recorded here.
Next time the same (or very similar) scenario appears, the agent looks it up
and applies the stored solution automatically — no human needed.

Over time this store becomes a playbook of every real obstacle encountered
on the Sarathi portal, growing richer with each run.
"""

import json
import hashlib
import aiosqlite
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
from pathlib import Path
from typing import Optional

from config.settings import get_settings

settings = get_settings()


class Scenario:
    """A situation the agent was in + the solution that worked."""

    def __init__(
        self,
        scenario_id: str,
        step_name: str,
        description: str,          # what the agent observed (text)
        page_url: str,
        solution: str,             # what action resolved it
        solution_detail: dict,     # structured: {action_type, selector_hint, value, notes}
        human_provided: bool,
        success_count: int = 1,
        fail_count: int = 0,
    ):
        self.scenario_id   = scenario_id
        self.step_name     = step_name
        self.description   = description
        self.page_url      = page_url
        self.solution      = solution
        self.solution_detail = solution_detail
        self.human_provided = human_provided
        self.success_count = success_count
        self.fail_count    = fail_count
        self.created_at    = _now()
        self.updated_at    = _now()

    def to_dict(self) -> dict:
        return self.__dict__

    @classmethod
    def from_dict(cls, d: dict) -> "Scenario":
        s = cls(
            scenario_id    = d["scenario_id"],
            step_name      = d["step_name"],
            description    = d["description"],
            page_url       = d.get("page_url", ""),
            solution       = d["solution"],
            solution_detail= d.get("solution_detail", {}),
            human_provided = d.get("human_provided", False),
            success_count  = d.get("success_count", 1),
            fail_count     = d.get("fail_count", 0),
        )
        s.created_at = d.get("created_at", "")
        s.updated_at = d.get("updated_at", "")
        return s


class LearningStore:
    """
    SQLite-backed store of past stuck-scenarios and their solutions.

    Lookup uses keyword overlap (no embedding model needed) — fast and good
    enough for the bounded domain of a single government portal.
    Swap _similarity() for a vector similarity fn if you want semantic search.
    """

    def __init__(self):
        self._db_path = settings.learning_db_path

    async def _ensure_db(self):
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS scenarios (
                    scenario_id   TEXT PRIMARY KEY,
                    step_name     TEXT NOT NULL,
                    description   TEXT NOT NULL,
                    page_url      TEXT,
                    solution      TEXT NOT NULL,
                    solution_detail TEXT NOT NULL,
                    human_provided INTEGER NOT NULL DEFAULT 1,
                    success_count INTEGER NOT NULL DEFAULT 1,
                    fail_count    INTEGER NOT NULL DEFAULT 0,
                    created_at    TEXT,
                    updated_at    TEXT
                )
            """)
            await db.commit()

    # ── Write ──────────────────────────────────────────────────────────────────

    async def record(self, scenario: Scenario):
        await self._ensure_db()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO scenarios
                   (scenario_id, step_name, description, page_url, solution,
                    solution_detail, human_provided, success_count, fail_count,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    scenario.scenario_id,
                    scenario.step_name,
                    scenario.description,
                    scenario.page_url,
                    scenario.solution,
                    json.dumps(scenario.solution_detail),
                    int(scenario.human_provided),
                    scenario.success_count,
                    scenario.fail_count,
                    scenario.created_at,
                    scenario.updated_at,
                ),
            )
            await db.commit()

    async def record_successful_action(
        self,
        step_name: str,
        observation: str,
        page_url: str,
        action_type: str,
        selector: str = "",
        text: str = "",
        value: str = "",
        tool_args: dict | None = None,
    ):
        """
        Store a concrete action that worked on a page.

        This is the "fifth attempt worked, try it first next time" memory.
        The id is based on the step, page and action shape so repeated wins
        reinforce the same scenario instead of creating many near-duplicates.
        """
        await self._ensure_db()
        action_key = json.dumps(
            {
                "step": step_name,
                "url": page_url.split("?")[0],
                "action_type": action_type,
                "selector": selector,
                "text": text,
                "value": value,
                "tool_args": tool_args or {},
            },
            sort_keys=True,
        )
        sid = self.make_scenario_id(step_name, f"WORKED:{action_key}")
        detail = {
            "action_type": action_type,
            "selector": selector,
            "text": text,
            "value": value,
            "tool_args": tool_args or {},
        }
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT scenario_id FROM scenarios WHERE scenario_id = ?", (sid,)
            ) as cur:
                exists = await cur.fetchone()

            if exists:
                await db.execute(
                    "UPDATE scenarios SET success_count = success_count + 1, "
                    "description = ?, solution = ?, solution_detail = ?, updated_at = ? "
                    "WHERE scenario_id = ?",
                    (
                        observation[:1000],
                        f"Reuse worked action: {action_type} selector={selector} text={text}",
                        json.dumps(detail),
                        _now(),
                        sid,
                    ),
                )
            else:
                await db.execute(
                    """INSERT INTO scenarios
                       (scenario_id, step_name, description, page_url, solution,
                        solution_detail, human_provided, success_count, fail_count,
                        created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        sid,
                        step_name,
                        observation[:1000],
                        page_url,
                        f"Reuse worked action: {action_type} selector={selector} text={text}",
                        json.dumps(detail),
                        0,
                        1,
                        0,
                        _now(),
                        _now(),
                    ),
                )
            await db.commit()

    async def mark_solution_outcome(self, scenario_id: str, worked: bool):
        """Track whether a recalled solution actually worked this time."""
        await self._ensure_db()
        col = "success_count" if worked else "fail_count"
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                f"UPDATE scenarios SET {col} = {col} + 1, updated_at = ? "
                f"WHERE scenario_id = ?",
                (_now(), scenario_id),
            )
            await db.commit()

    # ── Lookup ─────────────────────────────────────────────────────────────────

    async def find_solution(
        self, step_name: str, observation: str, page_url: str = ""
    ) -> Optional[Scenario]:
        """
        Find the best matching past scenario for the current situation.
        Returns None if nothing above the similarity threshold is found.
        """
        await self._ensure_db()
        candidates: list[Scenario] = []

        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                """SELECT * FROM scenarios
                   WHERE step_name = ?
                     AND success_count > fail_count
                     AND solution NOT LIKE 'DO NOT use:%'
                   ORDER BY success_count DESC""",
                (step_name,),
            ) as cursor:
                rows = await cursor.fetchall()
                cols = [d[0] for d in cursor.description]
                for row in rows:
                    d = dict(zip(cols, row))
                    d["solution_detail"] = json.loads(d["solution_detail"])
                    d["human_provided"]  = bool(d["human_provided"])
                    scenario = Scenario.from_dict(d)
                    if self._is_recallable_solution(scenario):
                        candidates.append(scenario)

        if not candidates:
            return None

        # Score by keyword overlap — good enough for bounded portal vocabulary
        # Give a small bonus when the stored scenario's page_url matches current
        best: Optional[Scenario] = None
        best_score = 0.0
        for c in candidates:
            score = self._similarity(observation, c.description)
            if page_url and c.page_url and c.page_url in page_url:
                score += 0.10   # small URL-match bonus; same Sarathi URL hosts many different states
            if c.success_count:
                score += min(c.success_count, 5) * 0.02
            if score > best_score:
                best_score = score
                best = c

        threshold = settings.scenario_similarity_threshold
        return best if best_score >= threshold else None

    @staticmethod
    def _is_recallable_solution(scenario: Scenario) -> bool:
        """
        Guardrail for self-learning.

        The store may contain historical "worked" actions from loops. Do not
        recall generic no-progress actions or navigation that exits the current
        application flow.
        """
        detail = scenario.solution_detail or {}
        action_type = (detail.get("action_type") or "").lower()
        text = (detail.get("text") or "").strip().lower()
        selector = (detail.get("selector") or "").strip().lower()

        if scenario.solution.startswith("Reuse worked action:"):
            if action_type in {"fill_many", "tool_call", "scroll", "wait"}:
                return False
            if action_type == "click" and text in {
                "change state",
                "dashboard",
                "home",
                "login",
            }:
                return False
            if selector in {"#change_state", "#dashboard", "#home"}:
                return False

        return True

    @staticmethod
    def _similarity(a: str, b: str) -> float:
        """Jaccard similarity on word tokens — fast, no model needed."""
        wa = set(a.lower().split())
        wb = set(b.lower().split())
        if not wa or not wb:
            return 0.0
        return len(wa & wb) / len(wa | wb)

    @staticmethod
    def make_scenario_id(step_name: str, description: str) -> str:
        raw = f"{step_name}:{description}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    # ── Bootstrap — seed known Sarathi quirks so agent starts smart ────────────

    async def seed_known_scenarios(self):
        """
        Pre-load known Sarathi portal behaviours discovered manually.
        This gives the agent a head start before any real runs.
        """
        known: list[dict] = [
            {
                "step_name":   "popup_close",
                "description": "mobile number update modal popup appears on homepage",
                "page_url":    "sarathi.parivahan.gov.in",
                "solution":    "Click the X close button or press Escape to dismiss",
                "solution_detail": {
                    "action_type":    "click",
                    "selector_hint":  "button or element with aria-label close or X symbol near top right of modal",
                    "value":          "",
                    "notes":          "This popup appears every time on the Sarathi homepage. Always dismiss before proceeding.",
                },
                "human_provided": False,
            },
            {
                "step_name":   "captcha_solve",
                "description": "CAPTCHA refresh or change link not found as button",
                "page_url":    "sarathi.parivahan.gov.in",
                "solution":    "Look for a text link saying Refresh or Change Image above or below the CAPTCHA",
                "solution_detail": {
                    "action_type":    "click",
                    "selector_hint":  "anchor link with text containing Refresh or Change near the CAPTCHA image",
                    "value":          "",
                    "notes":          "Sarathi uses a text anchor link not a button for CAPTCHA refresh.",
                },
                "human_provided": False,
            },
            {
                "step_name":   "auth_method_selection",
                "description": "two authentication options shown: mobile OTP or other method",
                "page_url":    "sarathi.parivahan.gov.in",
                "solution":    "Always select the mobile OTP authentication option",
                "solution_detail": {
                    "action_type":    "click",
                    "selector_hint":  "radio button or option labeled mobile number OTP or send OTP to mobile",
                    "value":          "mobile",
                    "notes":          "Prefer mobile OTP over Aadhaar OTP when both are shown — simpler flow.",
                },
                "human_provided": False,
            },
            {
                "step_name":   "alert_popup",
                "description": "browser alert dialog or confirmation box appears after form submission",
                "page_url":    "sarathi.parivahan.gov.in",
                "solution":    "Accept the alert by clicking OK",
                "solution_detail": {
                    "action_type":    "accept_alert",
                    "selector_hint":  "",
                    "value":          "",
                    "notes":          "Sarathi shows JS confirm() alerts at certain steps. Always accept unless it says Cancel Application.",
                },
                "human_provided": False,
            },
            {
                "step_name":   "session_timeout",
                "description": "session expired page or login required after long wait",
                "page_url":    "sarathi.parivahan.gov.in",
                "solution":    "Restart from state selection and replay completed steps from job checkpoint",
                "solution_detail": {
                    "action_type":    "restart",
                    "selector_hint":  "",
                    "value":          "",
                    "notes":          "Do NOT press browser back. Go to homepage and start fresh using saved job state.",
                },
                "human_provided": False,
            },
            {
                "step_name":   "payment_popup",
                "description": "fee payment page opens in a new popup window",
                "page_url":    "sarathi.parivahan.gov.in",
                "solution":    "Switch browser context to the new popup window and complete payment there",
                "solution_detail": {
                    "action_type":    "switch_window",
                    "selector_hint":  "",
                    "value":          "",
                    "notes":          "Playwright handles this via page.wait_for_event('popup'). Switch to new page.",
                },
                "human_provided": False,
            },
            {
                "step_name":   "close_state_popup",
                "description": "contactless_statepopup modal intercepts pointer events after state selection",
                "page_url":    "sarathi.parivahan.gov.in/sarathiservice/stateSelectBean.do",
                "solution":    "Close the contactless popup by clicking its X or pressing Escape, then proceed",
                "solution_detail": {
                    "action_type":    "close_popup",
                    "selector_hint":  "#contactless_statepopup .close, button[data-dismiss='modal']",
                    "value":          "",
                    "notes":          "After state selection, Sarathi shows a contactless services modal. Must be dismissed before any further navigation.",
                },
                "human_provided": False,
            },
            {
                "step_name":   "navigate_to_dl_services",
                "description": "contactless_statepopup modal still visible blocking DL services menu",
                "page_url":    "sarathi.parivahan.gov.in/sarathiservice/stateSelectBean.do",
                "solution":    "First close the contactless modal, then click Services on DL Renewal link",
                "solution_detail": {
                    "action_type":    "close_popup then click",
                    "selector_hint":  "a or li containing text 'Services on DL'",
                    "value":          "",
                    "notes":          "The contactless popup must be fully dismissed. After dismissal, find the DL Services menu item.",
                },
                "human_provided": False,
            },
            {
                "step_name":   "document_size_error",
                "description": "file size too large error shown after document upload",
                "page_url":    "sarathi.parivahan.gov.in",
                "solution":    "Compress the image using image_processor tool and re-upload",
                "solution_detail": {
                    "action_type":    "tool_call",
                    "selector_hint":  "",
                    "value":          "image_processor.compress",
                    "notes":          "Photo max 20KB, signature max 10KB on Sarathi.",
                },
                "human_provided": False,
            },
        ]

        for k in known:
            sid = self.make_scenario_id(k["step_name"], k["description"])
            await self.record(
                Scenario(
                    scenario_id    = sid,
                    step_name      = k["step_name"],
                    description    = k["description"],
                    page_url       = k.get("page_url", ""),
                    solution       = k["solution"],
                    solution_detail= k["solution_detail"],
                    human_provided = k["human_provided"],
                )
            )

    async def record_failure(
        self,
        step_name: str,
        observation: str,
        page_url: str,
        failed_approach: str,
    ):
        """
        Record that a particular approach failed so future runs can avoid it.
        Stored separately from successful scenarios — bumps fail_count so the
        approach gets deprioritized in find_solution scoring.
        """
        await self._ensure_db()
        sid = self.make_scenario_id(step_name, f"FAILED:{failed_approach}")
        async with aiosqlite.connect(self._db_path) as db:
            # If entry exists, just bump its fail_count
            async with db.execute(
                "SELECT scenario_id FROM scenarios WHERE scenario_id = ?", (sid,)
            ) as cur:
                exists = await cur.fetchone()

            if exists:
                await db.execute(
                    "UPDATE scenarios SET fail_count = fail_count + 1, updated_at = ? "
                    "WHERE scenario_id = ?",
                    (_now(), sid),
                )
            else:
                await db.execute(
                    """INSERT INTO scenarios
                       (scenario_id, step_name, description, page_url, solution,
                        solution_detail, human_provided, success_count, fail_count,
                        created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        sid, step_name,
                        f"FAILED approach: {failed_approach[:200]}",
                        page_url,
                        f"DO NOT use: {failed_approach}",
                        json.dumps({"failed_approach": failed_approach, "observation": observation[:200]}),
                        0, 0, 1,
                        _now(),
                        _now(),
                    ),
                )
            await db.commit()
