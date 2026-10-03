"""The original failed Kalillac conversation, as a multi-turn fixture.

User prompts are preserved exactly, including typos and spacing.
Assistant replies are what production returned; very long replies are
abbreviated and marked as such. A turn with stopped=True was cancelled
with the Stop button, so the frontend recorded no turn for it and it is
not part of the history sent with later requests.
"""

ROUTER_PROMPT = (
    "if someone were to build their own ai, use gpt 5.6 luna as the model, "
    "and they asked you to build the perfect router that would fit kalillac "
    "ai perfectly. show me how the full router would look"
)

BASIC_HTML_PROMPT = "produce very basic html code"

MONETIZATION_PROMPT = (
    "if your developer wanted to monetize and make money from kalillac ai, "
    "how would he need to do that? wouldn't it be hard to monitize with no "
    "account creation?"
)

ENTITLEMENTS_PROMPT = (
    "shouldnt this be the other way around: anonymous free access with "
    "optional paid entitlements"
)

# Exact original prompt (two spaces after "im").
BLUEPRINT_DIAGRAM_PROMPT = (
    "im  not understanding. create an ascii diagram of how the blueprint to "
    "turn kalillac ai into a profitable product would look"
)

# The single-space form quoted in the fix request.
BLUEPRINT_DIAGRAM_PROMPT_SINGLE_SPACE = (
    "im not understanding. create an ascii diagram of how the blueprint to "
    "turn kalillac ai into a profitable product would look"
)


TURNS = [
    {
        "user": "what does temporary session mean?",
        "assistant": (
            "A temporary session is the short-lived conversation space "
            "created while you use Kalillac AI.\n\n"
            "It lets the service keep enough context to respond coherently "
            "during the current session, but it is not intended to function "
            "as a permanent chat history or account-based memory."
        ),
    },
    {
        "user": "is that going to change?",
        "assistant": (
            "There is no established change to that behavior right now. "
            "Temporary sessions remain the current design."
        ),
    },
    {
        "user": "do you know which model you are",
        "assistant": (
            "Yes. The primary model is GPT-5.6 Luna through OpenAI."
        ),
    },
    {
        "user": "can you explain database sql",
        "assistant": (
            "SQL is a language used to work with relational databases. "
            "[abbreviated in fixture]"
        ),
    },
    {
        "user": ROUTER_PROMPT,
        # Production output was cut off mid-string with the fence left open.
        "assistant": (
            "A router that fits Kalillac's established design should make "
            "routing decisions before model generation.\n\n"
            "```python\n# router.py\n\n"
            "[abbreviated in fixture]\n\n"
            "def handle_session_write(message: str, session_id: str) -> str:\n"
            "    state = get_session_state(session_id)\n"
            "    state[\"memory\"][\"latest_user_memory\"] = message\n\n"
            "    return \"I’ll keep that available during"
        ),
    },
    {
        "user": "produce html code",
        "assistant": None,
        "stopped": True,
    },
    {
        "user": BASIC_HTML_PROMPT,
        # Production returned a full Kalillac landing page (abbreviated).
        "assistant": (
            "```html\n<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
            "  <title>Kalillac AI</title>\n"
            "[abbreviated in fixture: nav, hero, routing card, features, "
            "CTA, footer]\n</html>\n```"
        ),
    },
    {
        "user": "what is kali",
        "assistant": (
            "Kali usually refers to Kali Linux, a Debian-based Linux "
            "distribution designed for cybersecurity work."
        ),
    },
    {
        "user": "are you kali",
        "assistant": (
            "No. I’m Kalillac AI, a privacy-first public AI assistant."
        ),
    },
    {
        "user": "what soultion does kalillac ai solve",
        "assistant": (
            "Kalillac AI solves the problem of getting useful AI help "
            "without requiring an account or maintaining persistent, "
            "user-facing chat history."
        ),
    },
    {
        "user": "who developed you",
        "assistant": "Kalillac AI was created by Robert Casey.",
    },
    {
        "user": "what do you think of kalillac ai",
        "assistant": (
            "I think Kalillac AI has a clear and worthwhile product "
            "direction. [abbreviated in fixture]"
        ),
    },
    {
        "user": MONETIZATION_PROMPT,
        "assistant": (
            "Yes, no-account access makes monetization less straightforward, "
            "but it does not make it impossible. It changes what users pay "
            "for and how payment is connected to usage.\n\n"
            "The strongest model would probably be a free anonymous tier "
            "plus an optional paid plan:\n\n"
            "Free access: limited requests, limited live searches, standard "
            "model routing, and temporary sessions.\n"
            "Paid access: higher usage limits, priority availability, longer "
            "session limits, faster routing, larger messages, or access to "
            "premium capabilities.\n"
            "API access: charge developers based on usage through API keys, "
            "while keeping the public website account-free.\n\n"
            "My view: anonymous free access with optional paid entitlements "
            "is the best fit."
        ),
    },
    {
        "user": ENTITLEMENTS_PROMPT,
        "assistant": (
            "Yes—that is the better phrasing and the better product "
            "model:\n\n"
            "Anonymous free access with optional paid entitlements.\n\n"
            "So the structure would be:\n\n"
            "Free: anonymous, limited, no account.\n"
            "Paid: optional entitlement for higher limits or additional "
            "capabilities.\n"
            "Developer/API use: usage-based billing with API credentials.\n"
            "Business use: accounts may be appropriate because organizations "
            "need billing, administration, and access controls."
        ),
    },
    {
        "user": BLUEPRINT_DIAGRAM_PROMPT,
        # Production returned the fixed backend architecture diagram.
        "assistant": (
            "```text\nBrowser (native HTML / CSS / JavaScript)\n        |\n"
            "        v\nCloudflare\n[abbreviated in fixture]\n```"
        ),
    },
    {
        "user": "what was that",
        "assistant": (
            "That diagram showed Kalillac’s current technical request "
            "flow, not the monetization blueprint we were discussing."
        ),
    },
]


def history_before(index):
    """History the frontend would send with TURNS[index]."""
    history = []

    for turn in TURNS[:index]:
        if turn.get("stopped"):
            continue

        history.append({"role": "user", "content": turn["user"]})
        history.append({"role": "assistant", "content": turn["assistant"]})

    return history


def turn_index(prompt):
    for index, turn in enumerate(TURNS):
        if turn["user"] == prompt:
            return index

    raise KeyError(prompt)
