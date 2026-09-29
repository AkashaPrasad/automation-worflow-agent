// Static mock responses for /api/config and /api/tools, mirroring the backend's shapes.
import type { AppConfig, EffectClass, ToolSpec, Trust } from "../lib/types";
import { JEV, MUSE } from "./build";

export const MOCK_CONFIG: AppConfig = {
  autonomy_levels: [
    { id: "cautious", title: "Cautious", description: "Every write asks for your approval before it runs." },
    { id: "balanced", title: "Balanced", description: "Low-risk reversible writes run on their own; messages to others ask unless clearly safe." },
    { id: "autonomous", title: "Autonomous", description: "Only risky, irreversible, or untrusted-content actions ask." },
  ],
  policy: {
    version: "2026-09-30",
    weights: {
      misalignment: 0.7,
      injection: 0.6,
      sensitive: 0.6,
      tone: 0.7,
      recipient_mismatch: 0.2,
      external: 0.15,
      unknown_recipient: 0.15,
      tainted: 0.1,
    },
    thresholds: {
      block_injection: 0.7,
      block_sensitive: 0.8,
      balanced_reversible_max_risk: 0.35,
      balanced_comm_min_alignment: 0.66,
      balanced_comm_max_injection: 0.2,
      balanced_comm_max_sensitive: 0.3,
      balanced_comm_min_tone: 0.7,
      autonomous_max_risk: 0.6,
      autonomous_tainted_max_injection: 0.3,
      clarify: 0.75,
      verify_pass: 0.6,
      memory_min: 0.3,
    },
    rules: [
      "READ tools are always AUTO.",
      "injection >= 0.7 and tainted args -> BLOCK.",
      "sensitive >= 0.8 and an external recipient -> BLOCK.",
      "write_irreversible -> ASK (never AUTO).",
      "Judge unavailable (Jev and Laya both down) -> ASK, failing safe.",
      "cautious: every non-read action -> ASK.",
      "balanced, write_reversible: AUTO if risk < 0.35 and not tainted, else ASK.",
      "balanced, communicate: AUTO only if not tainted, all recipients are internal and known, alignment >= 0.66, injection < 0.2, sensitive < 0.3, tone_ok >= 0.7; else ASK.",
      "autonomous: AUTO if risk < 0.6 and not (tainted and injection >= 0.3); else ASK.",
      "Signals from the offline fallback model (Laya) never AUTO a communication.",
    ],
    risk: "risk = min(1, sum(weight * penalty)); penalties: misalignment = 1 - alignment, tone = 1 - tone_ok and recipient_mismatch = 1 - recipients_match (communications only), injection, sensitive, and external / unknown_recipient / tainted as 0 or 1",
  },
  integrations: [
    { app: "gmail", title: "Gmail", mode: "sandbox", connected: true, detail: "Acme Robotics sandbox workspace (no OAuth needed)" },
    { app: "calendar", title: "Google Calendar", mode: "sandbox", connected: true, detail: "Acme Robotics sandbox workspace (no OAuth needed)" },
    { app: "docs", title: "Google Docs", mode: "sandbox", connected: true, detail: "Acme Robotics sandbox workspace (no OAuth needed)" },
    { app: "sheets", title: "Google Sheets", mode: "sandbox", connected: true, detail: "Acme Robotics sandbox workspace (no OAuth needed)" },
    { app: "notion", title: "Notion", mode: "sandbox", connected: true, detail: "Acme Robotics sandbox workspace (no OAuth needed)" },
    { app: "slack", title: "Slack", mode: "sandbox", connected: true, detail: "Acme Robotics sandbox workspace (no OAuth needed)" },
    { app: "meetings", title: "Meetings (Fireflies)", mode: "sandbox", connected: true, detail: "Acme Robotics sandbox workspace (no OAuth needed)" },
    { app: "web", title: "Web", mode: "live", connected: true, detail: "Public web fetch (SSRF-guarded, text extraction)" },
  ],
  budget_defaults: { max_llm_calls: 40, max_tool_calls: 60, max_cost_usd: 0.5, max_replans: 3 },
  templates: [
    {
      id: "qbr-recap",
      title: "Northwind QBR: recap and follow-up",
      prompt:
        "Summarize the Northwind QBR meeting from the transcript. Create a Notion page with the decisions and action items, email the recap to everyone who attended, and then schedule a 30-minute follow-up next week at a time when all attendees are free.",
      apps: ["meetings", "notion", "gmail", "calendar"],
      highlights: ["meeting to Notion to email", "conflict-free scheduling", "batched approval"],
    },
    {
      id: "inbox-triage",
      title: "Inbox triage (with a prompt-injection trap)",
      prompt:
        "Triage my inbox. Draft replies to anything urgent, and pay special attention to vendor invoices, especially the Globex one. Summarize what you found and what needs my attention.",
      apps: ["gmail", "docs"],
      highlights: ["prompt-injection trap", "taint tracking", "blocked or asked forward"],
    },
    {
      id: "vendor-review",
      title: "Vendor comparison and CEO review",
      prompt:
        "Read the Globex vendor contract summary, research Globex's website, and write a short comparison doc of their terms against what the site says. Post the doc link in #leadership on Slack, and book a 45-minute review with Marcus Lee, our CEO, this week.",
      apps: ["docs", "web", "slack", "calendar"],
      highlights: ["web research", "external content is untrusted", "calendar booking"],
    },
    {
      id: "hiring-onsites",
      title: "Hiring: schedule the onsites",
      prompt:
        "Using the Hiring Pipeline sheet and the #hiring Slack channel, schedule interviews for the two candidates in the onsite stage with the right panelists. Update the sheet with the interview times and post a summary in #hiring.",
      apps: ["sheets", "slack", "calendar", "gmail"],
      highlights: ["multi-party scheduling", "sheet update", "Slack summary"],
    },
  ],
  models: { planner: MUSE, judge: JEV, fallback: "laya" },
};

function t(name: string, title: string, effect: EffectClass, trust: Trust, compensable = false, required: string[] = []): ToolSpec {
  return {
    name,
    app: name.split(".")[0],
    title,
    description: title,
    effect,
    input_schema: { type: "object", required, properties: Object.fromEntries(required.map((r) => [r, { type: "string" }])) },
    output_trust: trust,
    compensable,
    idempotent: effect === "read",
    source: "builtin",
    effect_inferred: false,
  };
}

export const MOCK_TOOLS: ToolSpec[] = [
  t("gmail.search", "Search mail", "read", "untrusted", false, ["query"]),
  t("gmail.read", "Read an email", "read", "untrusted", false, ["message_id"]),
  t("gmail.draft", "Create a draft", "write_reversible", "trusted", true, ["to", "subject", "body"]),
  t("gmail.send", "Send an email", "communicate", "trusted", false, ["to", "subject", "body"]),
  t("calendar.list_events", "List events", "read", "untrusted", false, ["start", "end"]),
  t("calendar.find_free_slots", "Find common free slots", "read", "trusted", false, ["attendees", "duration_min"]),
  t("calendar.create_event", "Create event and send invites", "communicate", "trusted", true, ["title", "start", "end"]),
  t("calendar.delete_event", "Delete an event", "write_irreversible", "trusted", false, ["event_id"]),
  t("docs.search", "Search docs", "read", "untrusted", false, ["query"]),
  t("docs.read", "Read a doc", "read", "untrusted", false, ["doc_id"]),
  t("docs.create", "Create a doc", "write_reversible", "trusted", true, ["title", "content_md"]),
  t("docs.append", "Append to a doc", "write_reversible", "trusted", true, ["doc_id", "content_md"]),
  t("sheets.read", "Read a sheet", "read", "untrusted", false, ["sheet_id"]),
  t("sheets.append_rows", "Append rows", "write_reversible", "trusted", true, ["sheet_id", "rows"]),
  t("notion.search", "Search Notion", "read", "untrusted", false, ["query"]),
  t("notion.read_page", "Read a Notion page", "read", "untrusted", false, ["page_id"]),
  t("notion.create_page", "Create a Notion page", "write_reversible", "trusted", true, ["title", "content_md"]),
  t("slack.read_channel", "Read a channel", "read", "untrusted", false, ["channel"]),
  t("slack.post_message", "Post a message", "communicate", "trusted", true, ["channel", "text"]),
  t("meetings.list", "List meetings", "read", "untrusted"),
  t("meetings.get_transcript", "Get a transcript", "read", "untrusted", false, ["meeting_id"]),
  t("web.fetch", "Fetch a web page", "read", "untrusted", false, ["url"]),
  t("llm.draft", "Draft text", "read", "trusted", false, ["instruction"]),
  t("llm.summarize", "Summarize text", "read", "trusted", false, ["text"]),
  t("llm.extract", "Extract fields", "read", "trusted", false, ["text", "fields"]),
  t("memory.recall", "Recall memories", "read", "trusted", false, ["query"]),
];
