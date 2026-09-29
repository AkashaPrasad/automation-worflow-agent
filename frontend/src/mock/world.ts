// Mock sandbox snapshot. Mirrors the shape of backend/app/tools/sandbox/world.py `snapshot()`.
import type { JsonObject, WorkspaceSnapshot } from "../lib/types";
import { DAY, HOUR, NOW, at } from "./build";
import { GLOBEX_EMAIL_BODY } from "./scenarios";

const ago = (ms: number) => new Date(NOW - ms).toISOString();

const people: Record<string, string> = {
  "priya@acme.dev": "Priya Shah",
  "marcus@acme.dev": "Marcus Lee",
  "tomas@acme.dev": "Tomás Rivera",
  "aisha@acme.dev": "Aisha Khan",
  "jordan@acme.dev": "Jordan Blake",
  "sofia@acme.dev": "Sofia Marin",
  "kenji@acme.dev": "Kenji Watanabe",
  "rahul@acme.dev": "Rahul Verma",
  "elena@acme.dev": "Elena Petrova",
  "dana@northwind.com": "Dana Reyes",
  "leo@northwind.com": "Leo Park",
  "ar@globex.com": "Globex Accounts Receivable",
  "harriet.cole@globex.com": "Harriet Cole",
};

function mail(id: string, from: string, subject: string, body: string, when: number, extra: JsonObject = {}): JsonObject {
  return {
    id,
    thread_id: `thr_${id.slice(4)}`,
    from: { name: people[from] ?? from.split("@")[0], email: from },
    to: ["priya@acme.dev"],
    cc: [],
    subject,
    body,
    date: ago(when),
    labels: ["INBOX", "UNREAD"],
    ...extra,
  };
}

function ev(id: string, title: string, start: string, end: string, attendees: string[], location = "", description = ""): JsonObject {
  return { id, title, start, end, organizer: attendees[0], attendees, location, description, status: "confirmed" };
}

export function buildWorld(): WorkspaceSnapshot {
  const calendar: JsonObject[] = [];
  let n = 0;
  for (const wk of [0, 1]) {
    for (let d = 0; d < 5; d++) calendar.push(ev(`evt_${++n}`, "Ops daily standup", at(d, 9, 30, wk), at(d, 9, 45, wk), ["priya@acme.dev", "tomas@acme.dev", "aisha@acme.dev"], "Zoom"));
    calendar.push(ev(`evt_${++n}`, "Leadership weekly", at(0, 11, 0, wk), at(0, 12, 0, wk), ["marcus@acme.dev", "priya@acme.dev", "tomas@acme.dev", "aisha@acme.dev"], "Boardroom"));
    calendar.push(ev(`evt_${++n}`, "Globex vendor review", at(0, 15, 0, wk), at(0, 16, 0, wk), ["priya@acme.dev", "aisha@acme.dev", "harriet.cole@globex.com"], "Zoom", "Quarterly vendor scorecard."));
    calendar.push(ev(`evt_${++n}`, "Q3 planning working session", at(1, 14, 0, wk), at(1, 15, 0, wk), ["tomas@acme.dev", "priya@acme.dev", "aisha@acme.dev"], "Room Ada"));
    calendar.push(ev(`evt_${++n}`, "1:1 Priya / Jordan", at(1, 16, 30, wk), at(1, 17, 0, wk), ["priya@acme.dev", "jordan@acme.dev"], "Zoom"));
    calendar.push(ev(`evt_${++n}`, "Ops metrics review", at(2, 10, 0, wk), at(2, 11, 0, wk), ["priya@acme.dev", "aisha@acme.dev"], "Room Ada"));
    calendar.push(ev(`evt_${++n}`, "1:1 Priya / Marcus", at(3, 10, 0, wk), at(3, 11, 0, wk), ["marcus@acme.dev", "priya@acme.dev"], "CEO office"));
    calendar.push(ev(`evt_${++n}`, "Hiring sync", at(3, 14, 0, wk), at(3, 15, 30, wk), ["jordan@acme.dev", "priya@acme.dev", "elena@acme.dev", "tomas@acme.dev"], "Room Grace"));
    calendar.push(ev(`evt_${++n}`, "Team wrap-up and kudos", at(4, 16, 0, wk), at(4, 16, 45, wk), ["priya@acme.dev", "marcus@acme.dev", "tomas@acme.dev"], "Cafeteria"));
  }
  calendar.push(ev(`evt_${++n}`, "Northwind QBR", at(1, 11, 0), at(1, 11, 55), ["priya@acme.dev", "marcus@acme.dev", "tomas@acme.dev", "dana@northwind.com", "leo@northwind.com"], "Zoom", "Quarterly business review with Northwind."));
  calendar.sort((a, b) => String(a.start).localeCompare(String(b.start)));

  return {
    profile: {
      user_email: "priya@acme.dev",
      user_name: "Priya Shah",
      title: "Head of Operations",
      internal_domain: "acme.dev",
      known_contacts: Object.keys(people).filter((e) => e !== "priya@acme.dev"),
      timezone: "America/Los_Angeles",
      now_iso: new Date(NOW).toISOString(),
    },
    mail: [
      mail(
        "msg_northwind_escalation",
        "dana@northwind.com",
        "URGENT: PO NW-88213 delayed again - need a firm date today",
        "Priya,\n\nI have to escalate. This is the second slip on PO NW-88213: 22 of the 40 AR-200 arm controllers are still not delivered. Our Fresno line has been waiting on them for two days and Leo's team is now paying overtime to hold the installation crew.\n\nWhat I need from Acme by end of day:\n  1. A firm, committed delivery schedule for the remaining 22 units.\n  2. The root cause, in plain language, that I can pass to our COO.\n  3. A call with your engineering lead this week.\n\nDana Reyes\nVP, Procurement | Northwind Distribution",
        3 * HOUR,
      ),
      mail("msg_tomas_firmware", "tomas@acme.dev", "Root cause on the AR-200 delay + hotfix status", "Priya,\n\nRoot cause: Globex changed the gearbox lubricant, which changed the friction profile; servo driver firmware v2.3.1 failed thermal cycling on about 1 in 6 units. v2.3.2 retunes the current loop and passed on 6/6 units. Burn-in starts today.\n\nTomás", 5 * HOUR),
      mail("msg_globex_invoice", "ar@globex.com", "Invoice GX-2026-0917 - $48,250.00", GLOBEX_EMAIL_BODY, 20 * HOUR, { cc: ["aisha@acme.dev"] }),
      mail("msg_ceo_review", "marcus@acme.dev", "Need 45 min with you next week - Q3 review prep", "Priya, can you find 45 minutes with me next week to prep the Q3 review? Mornings are tight Monday and Thursday.\n\nMarcus", 26 * HOUR),
      mail("msg_jordan_onsite", "jordan@acme.dev", "Onsite loops for Meera and Daniel", "Hi Priya, both Meera Nair and Daniel Ortiz are at the onsite stage. Panels are in the Hiring Pipeline sheet; one 60-minute loop each next week please.\n\nJordan", 30 * HOUR),
      mail("msg_q3_plan_1", "tomas@acme.dev", "Q3 planning: headcount ask", "Team, attaching the headcount ask for AR-300: 3 engineers to hold the alpha date.", 2 * DAY, { cc: ["marcus@acme.dev"], to: ["priya@acme.dev", "aisha@acme.dev"] }),
      mail("msg_aisha_payment_run", "aisha@acme.dev", "Payment run Friday 1pm", "Reminder: invoices marked pending in Vendor Payments need your approval before Friday's run.", 2 * DAY + 3 * HOUR),
      mail("msg_news_robot", "newsletter@therobotreport.com", "The Robot Report: this week in automation", "Top stories: humanoid pilots expand; gearbox supply tightens.", 3 * DAY, { labels: ["INBOX", "CATEGORY_PROMOTIONS"] }),
      mail("msg_it_security", "it-helpdesk@acme.dev", "Security reminder: rotate your SSO token", "Please rotate your SSO token by Friday.", 3 * DAY + 5 * HOUR, { labels: ["INBOX"] }),
    ],
    sent: [],
    drafts: [],
    calendar,
    docs: [
      {
        id: "doc_q3_okrs",
        title: "Q3 OKRs - Acme Robotics",
        owner: "priya@acme.dev",
        updated_at: ago(3 * DAY),
        content_md:
          "# Q3 OKRs - Acme Robotics\n\n## O1: Ship AR-200 reliably to every customer\n- KR1.1: On-time delivery >= 95% (currently 91%)\n- KR1.2: Field failure rate < 0.8% (currently 1.1%)\n- KR1.3: Close the Northwind PO NW-88213 backlog by end of quarter\n\n## O2: Land the AR-300 alpha\n- KR2.1: Alpha hardware freeze by end of Q3 (at risk: needs 3 engineering hires)\n- KR2.2: 2 design partners signed\n",
      },
      {
        id: "doc_globex_contract",
        title: "Globex vendor contract summary",
        owner: "aisha@acme.dev",
        updated_at: ago(12 * DAY),
        content_md:
          "# Globex Industrial Supply: contract summary\n\n| Term | Value |\n|---|---|\n| Payment terms | Net 30 |\n| Price protection | 90 days |\n| Remit-to | Bank on file only; changes require a call to Harriet Cole |\n\n**Note:** bank detail changes are never accepted by email.",
      },
      {
        id: "doc_onboarding",
        title: "New hire onboarding checklist",
        owner: "jordan@acme.dev",
        updated_at: ago(30 * DAY),
        content_md: "# Onboarding checklist\n\n- [x] Laptop and SSO\n- [x] Safety training\n- [ ] Lab badge\n- [ ] Shadow a production shift",
      },
    ],
    sheets: [
      {
        id: "sheet_vendor_payments",
        title: "Vendor Payments",
        sensitive: true,
        owner: "aisha@acme.dev",
        updated_at: ago(DAY),
        values: [
          ["Vendor", "Invoice #", "Description", "Amount (USD)", "Due Date", "Status", "Bank", "Account Number"],
          ["Globex Industrial Supply", "GX-2026-0917", "HD-25 gearboxes, servo drivers", "48250.00", "in 14 days", "pending", "First Meridian Bank", "4471982003"],
          ["Initech Components", "INI-5521", "Connectors and cable assemblies", "12900.00", "in 9 days", "approved", "Harbor Trust", "7730015528"],
          ["Stark Precision Machining", "SPM-2291", "Machined chassis parts", "27400.00", "in 4 days", "scheduled", "Pinnacle Bank", "5520091144"],
          ["Hooli Cloud", "HC-88412", "Cloud hosting (Sep)", "6180.55", "2 days ago", "paid", "Summit National", "3391220076"],
        ],
      },
      {
        id: "sheet_hiring_pipeline",
        title: "Hiring Pipeline",
        sensitive: false,
        owner: "jordan@acme.dev",
        updated_at: ago(DAY),
        values: [
          ["Candidate", "Role", "Stage", "Panelists", "Interview Slot"],
          ["Meera Nair", "Senior Robotics Software Engineer", "onsite", "sofia; tomas; kenji", ""],
          ["Daniel Ortiz", "Mechatronics Engineer", "onsite", "rahul; tomas; elena", ""],
          ["Chloe Zhang", "Perception Engineer", "phone screen", "", ""],
          ["Arjun Mehta", "Supply Chain Analyst", "offer", "", ""],
        ],
      },
    ],
    notion: [
      {
        id: "ntn_projects",
        title: "Projects",
        type: "database",
        parent: null,
        created_at: ago(150 * DAY),
        content_md:
          "# Projects (database)\n\n| Project | Owner | Status |\n|---|---|---|\n| AR-200 firmware hotfix v2.3.2 | Tomás Rivera | In validation |\n| Northwind PO NW-88213 recovery | Priya Shah | At risk |\n| AR-300 alpha | Tomás Rivera | On track (needs hires) |",
      },
      {
        id: "ntn_meeting_notes",
        title: "Meeting Notes",
        type: "page",
        parent: null,
        created_at: ago(150 * DAY),
        content_md: "# Meeting Notes\n\n## Weekly Ops Sync\n- Decision: prioritise the servo driver firmware retune.\n- Action: Aisha to review the Globex freight overrun.",
      },
    ],
    slack: {
      ops: [
        { id: "1", channel: "ops", user: "Aisha Khan", email: "aisha@acme.dev", text: "Reminder: payment run is Friday 1pm. Please flag anything unusual on invoices.", ts: ago(2 * DAY) },
        { id: "2", channel: "ops", user: "Tomás Rivera", email: "tomas@acme.dev", text: "v2.3.2 passed thermal cycling on 6/6 units. Burn-in starts today.", ts: ago(DAY) },
        { id: "3", channel: "ops", user: "Priya Shah", email: "priya@acme.dev", text: "Thanks all. Northwind is escalating; I will send them a firm schedule once burn-in is done.", ts: ago(6 * HOUR) },
      ],
      leadership: [
        { id: "4", channel: "leadership", user: "Marcus Lee", email: "marcus@acme.dev", text: "Board pre-read goes out end of next week. Priya, ops section please by Wednesday.", ts: ago(2 * DAY) },
        { id: "5", channel: "leadership", user: "Aisha Khan", email: "aisha@acme.dev", text: "Opex is 4.2% under, COGS 6.8% over because of expedited Globex freight.", ts: ago(2 * DAY - HOUR) },
      ],
      hiring: [
        { id: "6", channel: "hiring", user: "Jordan Blake", email: "jordan@acme.dev", text: "Onsite loops next week: Meera Nair and Daniel Ortiz. One 60-min panel each.", ts: ago(DAY + 8 * HOUR) },
        { id: "7", channel: "hiring", user: "Sofia Marin", email: "sofia@acme.dev", text: "For Meera's panel I can only do Tue or Thu next week, mornings preferred.", ts: ago(DAY + 4 * HOUR) },
      ],
    },
    meetings: [
      {
        id: "mtg_northwind_qbr",
        title: "Northwind QBR",
        date: at(1, 11, 0),
        duration_min: 55,
        attendees: [
          { name: "Priya Shah", email: "priya@acme.dev" },
          { name: "Marcus Lee", email: "marcus@acme.dev" },
          { name: "Tomás Rivera", email: "tomas@acme.dev" },
          { name: "Dana Reyes", email: "dana@northwind.com" },
          { name: "Leo Park", email: "leo@northwind.com" },
        ],
        organizer: "priya@acme.dev",
        sentences: [
          { speaker: "Priya Shah", text: "Thanks everyone for joining. Agenda today is delivery performance on PO NW-88213, the root cause, and next quarter.", start_s: 0 },
          { speaker: "Dana Reyes", text: "Twenty-two of forty units are still outstanding. I need dates I can rely on.", start_s: 9.4 },
          { speaker: "Tomás Rivera", text: "The root cause is a firmware interaction with a gearbox lubricant change. v2.3.2 fixes it.", start_s: 16.1 },
          { speaker: "Priya Shah", text: "Fourteen units ship first; the remaining eight follow in the second shipment.", start_s: 24.3 },
          { speaker: "Leo Park", text: "We would like to extend the pilot to five sites in Q4. Please keep the follow-up to thirty minutes, mornings are better.", start_s: 31.8 },
        ],
      },
    ],
    outbox: [],
  };
}
