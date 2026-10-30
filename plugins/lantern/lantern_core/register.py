"""How the user talks, learned from their own messages and kept in settings: plain words unless they show they are
an expert. "The plex thing" is a non-technical reader however many services run on the machine; a pasted unit name,
a command or "I'm a developer" is an expert. Unsure means plain. Their own names for things are kept too, so Lantern
says "the plex thing" back rather than container-plex.service."""
import re

from . import discovery

PLAIN = re.compile(r"\b(?:the \w+ thing|keep it simple|plain (?:words|english)|(?:in )?english please|(?:too|not (?:very )?)technical|non-technical|"
                   r"what does that mean|in simple terms|explain (?:it )?like|eli5|layman|dumb it down|"
                   r"I'?m not a (?:techie|developer|programmer|computer person)|just fix it)\b", re.I)
EXPERT = re.compile(r"(?:^|\s)(?:~/|/etc/|/var/|systemctl|journalctl|podman|docker|git (?:status|log|diff|rebase)|\S+\.(?:service|timer)\b)|`[^`]{3,}`|"
                    r"\bI'?m an? (?:developer|programmer|engineer|sysadmin|devops|admin)\b", re.I)
OWN_NAME = re.compile(r"\b((?:the|my) (\w+) thing)\b", re.I)
UNIT = re.compile(r"\b([\w.@-]+?)\.(service|timer)\b")


def model() -> dict:
    return discovery.settings().get("register") or {"level": "plain", "names": {}, "said": []}


def level() -> str:
    """plain or expert. Nothing the machine shows can make a reader an expert; only their words can."""
    return model().get("level", "plain")


def learn(prompt: str) -> None:
    """One message at a time: a plain-words signal wins for good (a path pasted later does not undo "keep it simple");
    an expert signal counts only while no plain signal was ever given."""
    m = model()
    plain, expert = PLAIN.search(prompt), EXPERT.search(prompt)
    if plain:
        m["level"], m["plain_said"] = "plain", True
    elif expert and not m.get("plain_said"):
        m["level"] = "expert"
    for whole, word in OWN_NAME.findall(prompt):
        m.setdefault("names", {})[word.lower()] = whole.lower()
    said = [s for s in (plain, expert) if s]
    if said or OWN_NAME.search(prompt):
        m["said"] = (m.get("said", []) + [prompt.strip()[:120]])[-5:]
        discovery.save_settings({**discovery.settings(), "register": m})


def names() -> dict[str, str]:
    """{capability name: what the user calls it}, for the things the user has named in their own words."""
    own = model().get("names", {})
    out = {}
    for cap in discovery.settings().get("capabilities", {}).values():
        key = next((w for w in own if w in cap["name"].lower()), None)
        if key:
            out[cap["name"]] = own[key]
    return out


def say(text: str) -> str:
    """Text the user sees without a model in between (the status line): unit names become plain, their own names used."""
    if level() == "expert":
        return text
    own = names()

    def unit(m):
        for cap, name in own.items():
            if cap.lower() in m.group(1).lower():
                return name
        return f"the {m.group(1)} {'job' if m.group(2) == 'timer' else 'service'}"
    text = UNIT.sub(unit, text)
    for cap, name in own.items():  # a bare "Plex" too, but not the one inside "the plex thing" just placed
        text = re.sub(rf"(?<!the )(?<!my )\b{re.escape(cap)}\b(?! thing)", name, text, flags=re.I)
    return text


def guidance() -> str:
    """The register line for a session's context: how to write, and the user's own names for things."""
    own = ", ".join(f'"{v}" for {k}' for k, v in sorted(names().items()))
    if level() == "expert":
        return ("Write for an expert (they used paths, units or commands themselves): paths, units and line numbers inline, "
                "mechanism stated plainly; question options name the exact unit or file.")
    return ("Write for a non-technical reader (the default until they use paths, units or commands themselves): consequence "
            "first in plain words, mechanism in one clause, no unit names, paths, commands or flags in the text (a trailing "
            "'details:' line at most, only when they ask), question options in everyday words, one idea each."
            + (f" Their own names for things, use them: {own}." if own else ""))
