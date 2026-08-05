# Trigger regexes and reply text for services/chitchat.py — kept separate from the matching
# logic so wording/phrasing changes don't require touching the dispatch code.

GREETING_PATTERN = r"^(hi|hello|hey|yo|hola|good morning|good evening|good afternoon)\b[!.]*$"
ALIVE_PATTERN = r"^(are you (alive|there|awake|up)|you (there|up)|alive|u there|u up)\??$"
WHOAMI_PATTERN = r"^(who|what) are you\??$"
HELP_PATTERN = r"^(help|what can you do)\??$"
THANKS_PATTERN = r"^(thanks|thank you|thx|ty)\b[!.]*$"
BYE_PATTERN = r"^(bye|goodbye|see ya|see you)\b[!.]*$"
TIME_PATTERN = r"^(what'?s?\s+(the\s+|a\s+)?time(\s+(is\s+it|now))?|time\s+now)\??$"

GREETING_REPLY = "Hey! I'm Jacob — send me anything you want to remember, or ask what's open."
ALIVE_REPLY = "Yep, I'm here and running."
ABOUT_REPLY = (
    "I'm Jacob, your personal notes & tasks assistant. Tell me things to remember, ask what's "
    'open, or save a quick fact like "save my wifi password: ...".'
)
THANKS_REPLY = "Anytime!"
BYE_REPLY = "See you later!"
