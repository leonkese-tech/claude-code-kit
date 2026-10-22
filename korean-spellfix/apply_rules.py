"""Apply the rules in rules.jsonl to Korean text (stdlib only).

Order of work: (1) mask protected spans, (2) apply every rule on the masked text
only, (3) splice replacements into the original. Protected spans are therefore
never edited, and a rule can never match across them.
"""
import json
import os
import re
import sys

MASK = "\u00a7"  # '§' - a char no rule pattern matches; also used by lookbehinds
HERE = os.path.dirname(os.path.abspath(__file__))

PROTECT = [
    re.compile(r"https?://\S+|www\.\S+"),                      # URLs
    re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),               # e-mail
    re.compile(r"\[[^\]\n]{1,40}\]"),                          # [tags]
    re.compile(r"\"[^\"\n]*\"|“[^”\n]*”|«[^»\n]*»|‘[^’\n]*’"),  # quoted text
    re.compile(r"「[^」\n]*」|『[^』\n]*』"),                      # corner-bracket quotes
    # Straight single quotes, only where they clearly delimit a quote: the opening
    # one is not glued to a preceding word character (so don't / it's never open a
    # quote) and the closing one is not glued to a following ASCII word character.
    re.compile(r"(?<![\w'])'(?=\S)(?:[^'\n]|(?<=[A-Za-z])'(?=[A-Za-z]))*?(?<=\S)'(?![A-Za-z0-9_])"),
    re.compile(r"(?m)^[ \t]*[\w가-힣 ]{1,20}[ \t]*:[ \t]*\S.*$"),  # 'key : value' lines
    re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-./:+#]*"),            # ASCII words, numbers, SKU-like codes
]


def load_rules(path=None):
    path = path or os.path.join(HERE, "rules.jsonl")
    rules = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                r["_re"] = re.compile(r["pattern"])
                r["_na"] = re.compile(r["not_after"]) if r.get("not_after") else None
                rules.append(r)
    return rules


def mask(text):
    chars = list(text)
    for rx in PROTECT:
        for m in rx.finditer("".join(chars)):
            chars[m.start():m.end()] = MASK * (m.end() - m.start())
    return "".join(chars)


def _has_final_not_rieul(ch):
    if not ("\uac00" <= ch <= "\ud7a3"):
        return False
    jong = (ord(ch) - 0xAC00) % 28
    return jong not in (0, 8)  # no final consonant, or 'ㄹ'


def apply(text, rules=None):
    rules = rules if rules is not None else load_rules()
    orig, masked = text, mask(text)
    for r in rules:
        o_parts, m_parts, pos = [], [], 0
        for m in r["_re"].finditer(masked):
            if r.get("prev_jong") and not (m.start() > 0 and _has_final_not_rieul(orig[m.start() - 1])):
                continue
            if r["_na"] and r["_na"].search(orig[max(0, m.start() - 40):m.start()]):
                continue
            new = m.expand(r["replacement"])
            o_parts += [orig[pos:m.start()], new]
            m_parts += [masked[pos:m.start()], new]
            pos = m.end()
        orig = "".join(o_parts) + orig[pos:]
        masked = "".join(m_parts) + masked[pos:]
    return orig


if __name__ == "__main__":
    data = sys.stdin.read() if len(sys.argv) < 2 else open(sys.argv[1], encoding="utf-8").read()
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(apply(data))
