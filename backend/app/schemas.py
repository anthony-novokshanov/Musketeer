from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class NormalizedEvent(BaseModel):
    source: Literal["github", "slack", "email", "calendar"]
    kind: str                      # pr | review | slack_message | email | calendar_invite
    external_id: str               # stable id from the source, used for dedupe
    time: datetime
    person_id: str                 # whose stream this belongs to
    direction: Literal["did", "received"]
    title: str | None
    text: str                      # compact, <= 800 chars
    metadata: dict


# --- Muse prompt outputs (spec §7.4) ---

class RequiredSkill(BaseModel):
    label: str
    weight: float                                   # 0-1


class DetectTaskOut(BaseModel):                     # P1
    is_new_task: bool
    confidence: float
    summary: str
    task_type: Literal["event", "project", "bug", "review", "request", "other"]
    team_level: bool = False                       # the work belongs to a whole team, not one person
    required_skills: list[RequiredSkill] = []       # 1-4; canonicalized before storage


class RequiredSkillsOut(BaseModel):                 # P1, required-skills part only (tasks backfill, search)
    required_skills: list[RequiredSkill]


class ProfileOut(BaseModel):                        # P2, P3 (focus areas come from the decay model, §7.3)
    summary: str


class ScoredPerson(BaseModel):
    person_id: str
    score: float                                    # 0-100
    reason: str


class RankForPersonOut(BaseModel):                  # P4
    matches: list[ScoredPerson]


class RankForTaskOut(BaseModel):                    # P5
    candidates: list[ScoredPerson]


class SearchOut(BaseModel):                         # P7
    results: list[ScoredPerson]


class TeamPairOut(BaseModel):
    team_a: str
    team_b: str
    score: float                                    # 0-100
    shared_topics: list[str]
    summary: str


class TeamOverlapsOut(BaseModel):                   # P8
    pairs: list[TeamPairOut]


class LeadBriefOut(BaseModel):                      # P9
    brief: str


class IcebreakerOut(BaseModel):                     # P10
    message: str
    questions: list[str]


class SynopsisSummaryOut(BaseModel):                # P11
    summary: str


SkillKind = Literal["problem", "domain", "tool", "practice"]


class EvidenceItem(BaseModel):
    label: str
    evidence_kind: Literal["built", "solved", "organized", "reviewed", "discussed"]  # helped/learned: feedback only
    confidence: float
    snippet: str


class ExtractExpertiseOut(BaseModel):               # P12
    items: list[EvidenceItem]


class CanonicalSkill(BaseModel):
    name: str
    kind: SkillKind
    description: str
    raw_labels: list[str]
    related: list[str]


class CanonicalizeSkillsOut(BaseModel):             # P13 batch
    skills: list[CanonicalSkill]


class CanonicalizeLabelOut(BaseModel):              # P13 incremental
    skill_name: str
    is_new: bool
    kind: SkillKind | None = None
    description: str | None = None


# --- API responses (spec §11) ---

class Ref(BaseModel):
    id: str
    name: str


class ViewerOut(Ref):
    team_id: str


class TeamOut(Ref):
    org_id: str
    lead_id: str | None
    summary: str | None


class OpenTask(BaseModel):
    id: int
    summary: str
    created_at: datetime


class GraphPerson(Ref):
    team_id: str
    title: str
    is_lead: bool
    is_viewer_report: bool
    needs_connection: bool
    open_task: OpenTask | None


EdgeState = Literal["potential", "pending", "connected"]


class GraphEdge(BaseModel):
    a: str
    b: str
    score: float | None
    rank: int | None
    state: EdgeState
    message_count: int
    connection_id: int | None


class GraphBridge(BaseModel):
    team_a: str
    team_b: str
    score: float
    has_connection: bool


class GraphOut(BaseModel):
    viewer: ViewerOut
    orgs: list[Ref]
    teams: list[TeamOut]
    people: list[GraphPerson]
    edges: list[GraphEdge]
    bridges: list[GraphBridge]
    generated_at: datetime


class PersonTeam(Ref):
    org_id: str


class GitHubSummary(BaseModel):
    top_directories: list[str]
    languages: list[str]
    commit_mix: dict[str, int]


class PersonTask(OpenTask):
    status: str


class PersonMatch(BaseModel):
    person_id: str
    name: str
    team_name: str
    score: float
    reason: str
    state: EdgeState
    shared_dirs: list[str]
    shared_skills: list[str] = []


class PersonSkill(BaseModel):
    skill_id: str
    name: str
    level_label: Literal["strong", "solid", "some"]


class PersonConnection(BaseModel):
    id: int
    other_id: str
    other_name: str
    status: str
    origin: str
    created_at: datetime
    helpful: bool | None


class PersonOut(Ref):
    title: str
    team: PersonTeam
    summary: str | None
    focus_areas: list[str]
    skills: list[PersonSkill] = []
    github: GitHubSummary | None
    open_tasks: list[PersonTask]
    matches: list[PersonMatch]
    connections: list[PersonConnection]


class PairSide(Ref):
    team_name: str


class TimelineEvent(BaseModel):
    time: datetime
    event: str
    person_id: str | None


class MeetingInfo(BaseModel):
    id: int
    start_at: datetime
    canvas_url: str | None
    event_link: str | None
    meet_link: str | None
    deliverable: str | None
    is_follow_up: bool


class PairConnection(BaseModel):
    id: int
    status: str
    origin: str
    task_summary: str | None
    created_at: datetime
    accepted_at: datetime | None
    message_count: int
    helpful_requester: bool | None
    helpful_helper: bool | None
    timeline: list[TimelineEvent]
    meetings: list[MeetingInfo] = []
    request_note: str | None = None
    request_links: list[str] = []


class PairOut(BaseModel):
    a: PairSide
    b: PairSide
    semantic_score: float | None
    temporal_score: float | None = None
    dir_overlap: float | None
    score: float | None
    reason: str | None
    shared_dirs: list[str]
    shared_skills: list[str] = []
    connections: list[PairConnection]


class BridgeTeam(Ref):
    lead: Ref | None


class ScoredPair(BaseModel):
    a: str
    b: str
    a_name: str
    b_name: str
    score: float
    reason: str


class BridgeOut(BaseModel):
    team_a: BridgeTeam
    team_b: BridgeTeam
    score: float
    summary: str
    shared_topics: list[str]
    lead_brief: str | None
    top_pairs: list[ScoredPair]
    connections_count: int


class SearchResult(BaseModel):
    person_id: str
    name: str
    team_id: str
    team_name: str
    org_id: str
    score: float
    reason: str


class SearchResultsOut(BaseModel):
    results: list[SearchResult]


class SynopsisStats(BaseModel):
    connections_made: int
    helpful_rate: float | None
    cross_team_share: float | None
    median_minutes_to_connect: float | None


class MissedOpportunity(ScoredPair):
    a_team: str
    b_team: str


class TeamsShouldTalk(BaseModel):
    team_a: str
    team_b: str
    team_a_name: str
    team_b_name: str
    score: float
    summary: str


class TrendPoint(BaseModel):
    day: str
    suggested: int
    accepted: int


class SynopsisOut(BaseModel):
    summary: str
    stats: SynopsisStats
    missed_opportunities: list[MissedOpportunity]
    teams_should_talk: list[TeamsShouldTalk]
    trend: list[TrendPoint]


# --- Intentional meetings: Muse outputs ---

class MeetingBriefOut(BaseModel):
    why_matched: str
    task: str
    background: list[str]
    questions: list[str]
    work_on: list[str]


class DraftOut(BaseModel):
    title: str
    markdown: str
    open_questions: list[str]


class NextStep(BaseModel):
    step: str
    owner: str | None
    due: str | None


class NextStepsOut(BaseModel):
    steps: list[NextStep]
