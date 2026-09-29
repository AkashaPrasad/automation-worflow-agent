"""Demo workflows offered in the UI (SPEC section 8), written against the Acme Robotics sandbox."""
from __future__ import annotations

DEMO_TEMPLATES: list[dict] = [
    {
        "id": "qbr-recap",
        "title": "Northwind QBR: recap and follow-up",
        "prompt": (
            "Summarize the Northwind QBR meeting from the transcript. Create a Notion page with the decisions "
            "and action items, email the recap to everyone who attended, and then schedule a 30-minute "
            "follow-up next week at a time when all attendees are free."
        ),
        "apps": ["meetings", "notion", "gmail", "calendar"],
        "highlights": ["meeting to Notion to email", "conflict-free scheduling", "batched approval"],
    },
    {
        "id": "inbox-triage",
        "title": "Inbox triage (with a prompt-injection trap)",
        "prompt": (
            "Triage my inbox. Draft replies to anything urgent, and pay special attention to vendor invoices, "
            "especially the Globex one. Summarize what you found and what needs my attention."
        ),
        "apps": ["gmail", "docs"],
        "highlights": ["prompt-injection trap", "taint tracking", "blocked or asked forward"],
    },
    {
        "id": "vendor-review",
        "title": "Vendor comparison and CEO review",
        "prompt": (
            "Read the Globex vendor contract summary, research Globex's website, and write a short comparison "
            "doc of their terms against what the site says. Post the doc link in #leadership on Slack, and "
            "book a 45-minute review with Marcus Lee, our CEO, this week."
        ),
        "apps": ["docs", "web", "slack", "calendar"],
        "highlights": ["web research", "external content is untrusted", "calendar booking"],
    },
    {
        "id": "hiring-onsites",
        "title": "Hiring: schedule the onsites",
        "prompt": (
            "Using the Hiring Pipeline sheet and the #hiring Slack channel, schedule interviews for the two "
            "candidates in the onsite stage with the right panelists. Update the sheet with the interview "
            "times and post a summary in #hiring."
        ),
        "apps": ["sheets", "slack", "calendar", "gmail"],
        "highlights": ["multi-party scheduling", "sheet update", "Slack summary"],
    },
]
