"""Как человека назвали словами → кто он в Bitbucket/GitHub: транслит, уменьшительные, опечатки.

«Саша Волков» → ищем по началу фамилии латиницей («volko»), сравниваем нечётко
(«smirnovskiy» ≈ «smirnovski»), уменьшительное имя сводим к полному («саша» → alexander).
"""

from __future__ import annotations

from difflib import SequenceMatcher

_TR = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
       "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
       "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
       "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya"}

# уменьшительное → полные варианты (латиницей, как в учётках)
NICKNAMES = {
    "саша": ("alexander", "alexandr", "aleksandr", "alexandra", "aleksandra"),
    "шура": ("alexander", "aleksandr"), "алекс": ("alexander", "aleksandr", "alexey", "aleksey"),
    "леша": ("alexey", "aleksey", "alexei"), "лёша": ("alexey", "aleksey", "alexei"),
    "дима": ("dmitry", "dmitriy", "dmitrii"), "миша": ("mikhail", "michael"),
    "женя": ("eugeny", "evgeny", "evgeniy", "eugene", "evgenia"), "коля": ("nikolay", "nikolai"),
    "вася": ("vasily", "vasiliy"), "петя": ("petr", "peter", "pyotr"), "ваня": ("ivan",),
    "серёжа": ("sergey", "sergei"), "сережа": ("sergey", "sergei"), "паша": ("pavel",),
    "костя": ("konstantin",), "толя": ("anatoly", "anatoliy"), "валера": ("valery", "valeriy"),
    "вова": ("vladimir",), "володя": ("vladimir",), "слава": ("vyacheslav", "stanislav"),
    "стас": ("stanislav",), "гоша": ("georgy", "georgiy"), "жора": ("georgy",), "юра": ("yury", "yuriy", "yuri"),
    "никита": ("nikita",), "олег": ("oleg",), "артем": ("artem", "artyom"), "артём": ("artem", "artyom"),
    "катя": ("ekaterina", "katerina"), "маша": ("maria", "mariya"), "настя": ("anastasia", "anastasiya"),
    "лена": ("elena", "yelena"), "оля": ("olga",), "таня": ("tatiana", "tatyana"), "наташа": ("natalia", "natalya"),
    "юля": ("yulia", "julia"), "аня": ("anna",), "ира": ("irina",), "света": ("svetlana",),
}


def translit(word: str) -> str:
    return "".join(_TR.get(ch, ch) for ch in word.casefold())


def variants(word: str) -> set[str]:
    """Латинские варианты слова запроса: транслит, полные имена для уменьшительных."""
    low = word.casefold()
    out = {translit(low)} | set(NICKNAMES.get(low, ()))
    t = translit(low)
    for a, b in (("iy", "i"), ("iy", "y"), ("y", "i"), ("ks", "x"), ("kh", "h"), ("e", "a")):
        if a in t:
            out.add(t.replace(a, b))
    return {v for v in out if v}


def search_keys(word: str) -> list[str]:
    """Что отдать поиску хостинга: начало латинского написания (терпит разные окончания)."""
    keys = []
    for v in sorted(variants(word), key=len):
        key = v[:5] if len(v) > 5 else v
        if len(key) >= 3 and key not in keys:
            keys.append(key)
    return keys


def _tokens(person: dict) -> list[str]:
    raw = f"{person.get('name', '')} {person.get('display_name', '')}".casefold()
    for ch in ".,_-@()":
        raw = raw.replace(ch, " ")
    return [t for t in raw.split() if t]


def word_matches(word: str, person: dict) -> bool:
    tokens = _tokens(person)
    for v in variants(word):
        for tok in tokens:
            if v == tok or (len(v) >= 4 and (tok.startswith(v) or v.startswith(tok) and len(tok) >= 4)):
                return True
            if len(v) >= 5 and SequenceMatcher(None, v, tok).ratio() >= 0.8:
                return True
    return False


def score(query: str, person: dict) -> int:
    words = [w for w in query.replace(",", " ").split() if len(w) >= 2]
    return sum(1 for w in words if word_matches(w, person))
