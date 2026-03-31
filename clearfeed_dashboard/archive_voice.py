from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from statistics import mean
from typing import Any


PHRASE_CANDIDATES = [
    "actually",
    "genuinely",
    "most",
    "everyone",
    "nobody",
    "product",
    "builder",
    "memory",
    "continuity",
    "presence",
    "trust",
    "the real story",
    "wrong question",
    "it's not",
    "not because",
]


REPRESENTATIVE_PATTERNS = [
    "The most underbuilt thing in AI right now is not intelligence",
    "Everyone is building AI assistants",
    "The full circle is ironic",
    "Google VP Darren Mowry warned",
    "Anthropic just shipped",
    "When people ask about my startup",
]


def resolve_archive_dir(path: Path) -> Path:
    candidate = path.expanduser().resolve()
    if (candidate / "data").exists():
        return candidate
    if candidate.name == "data" and (candidate / "tweets.js").exists():
        return candidate.parent
    raise FileNotFoundError(
        "Archive folder not found or unsupported. Point Clearfeed at the unzipped X archive root folder."
    )


def load_js_assignment(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    if "=" not in text:
        raise ValueError(f"Expected JS assignment in {path}")
    payload = text.split("=", 1)[1].strip().rstrip(";").strip()
    return json.loads(payload)


def normalize_text(text: str) -> str:
    return " ".join(text.split()).strip()


def collect_archive_items(archive_dir: Path) -> list[dict[str, str]]:
    data_dir = archive_dir / "data"
    tweets_path = data_dir / "tweets.js"
    if not tweets_path.exists():
        raise FileNotFoundError("Archive is missing data/tweets.js.")

    items: list[dict[str, str]] = []

    for row in load_js_assignment(tweets_path):
        tweet = row.get("tweet", {})
        text = (tweet.get("full_text") or "").strip()
        if not text or tweet.get("retweeted") or text.startswith("RT @"):
            continue
        kind = "reply" if tweet.get("in_reply_to_status_id_str") else "tweet"
        items.append({"kind": kind, "text": text})

    notes_path = data_dir / "note-tweet.js"
    if notes_path.exists():
        for row in load_js_assignment(notes_path):
            note = row.get("noteTweet", {})
            text = (note.get("core", {}).get("text") or "").strip()
            if text:
                items.append({"kind": "note", "text": text})

    community_path = data_dir / "community-tweet.js"
    if community_path.exists():
        for row in load_js_assignment(community_path):
            tweet = row.get("tweet", {})
            text = (tweet.get("full_text") or "").strip()
            if not text or tweet.get("retweeted") or text.startswith("RT @"):
                continue
            items.append({"kind": "community", "text": text})

    deduped: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        normalized = normalize_text(item["text"])
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append({"kind": item["kind"], "text": item["text"]})
    return deduped


def first_word_counts(texts: list[str]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for text in texts:
        words = text.replace("\n", " ").split()
        if words:
            counts[words[0]] += 1
    return counts


def first_bigram_counts(texts: list[str]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for text in texts:
        words = text.replace("\n", " ").split()
        if len(words) >= 2:
            counts[" ".join(words[:2])] += 1
    return counts


def count_phrase_hits(texts: list[str]) -> dict[str, int]:
    lowered = [text.lower() for text in texts]
    return {phrase: sum(1 for text in lowered if phrase in text) for phrase in PHRASE_CANDIDATES}


def is_emoji_heavy(text: str) -> bool:
    return any(ord(char) > 10000 for char in text)


def clean_excerpt(text: str, limit: int = 170) -> str:
    normalized = normalize_text(text)
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def choose_representative_examples(texts: list[str]) -> list[str]:
    chosen: list[str] = []
    for pattern in REPRESENTATIVE_PATTERNS:
        match = next((text for text in texts if pattern.lower() in text.lower()), None)
        if match:
            chosen.append(clean_excerpt(match))
    if len(chosen) < 6:
        for text in texts:
            if 100 <= len(normalize_text(text)) <= 240:
                excerpt = clean_excerpt(text)
                if excerpt not in chosen:
                    chosen.append(excerpt)
            if len(chosen) >= 6:
                break
    return chosen[:6]


def render_archive_summary(archive_dir: Path, items: list[dict[str, str]]) -> str:
    by_kind: dict[str, list[str]] = {
        "tweet": [item["text"] for item in items if item["kind"] == "tweet"],
        "reply": [item["text"] for item in items if item["kind"] == "reply"],
        "note": [item["text"] for item in items if item["kind"] == "note"],
        "community": [item["text"] for item in items if item["kind"] == "community"],
    }
    originals = [
        text
        for item in items
        if item["kind"] in {"tweet", "note", "community"}
        for text in [item["text"]]
        if not text.startswith("@")
    ]
    all_texts = [item["text"] for item in items]
    phrase_hits = count_phrase_hits(all_texts)
    first_words = first_word_counts(originals)
    first_bigrams = first_bigram_counts(originals)
    examples = choose_representative_examples(originals)

    questions = sum(1 for text in originals if "?" in text)
    exclaims = sum(1 for text in originals if "!" in text)
    emoji_posts = sum(1 for text in originals if is_emoji_heavy(text))
    multiline_posts = sum(1 for text in originals if "\n" in text)
    lowercase_i = sum(1 for text in originals if re.search(r"\bi\b", text) is not None)
    reply_average = round(mean(len(text) for text in by_kind["reply"]), 1) if by_kind["reply"] else 0.0

    lines = [
        "# ARCHIVE VOICE",
        "",
        "This file is generated from the user's imported X archive.",
        "It should be treated as a high-signal reference when proposing updates to Voice.md.",
        "",
        "## Corpus Basis",
        "",
        f"- Source archive: `{archive_dir.name}`",
        f"- Deduped authored items: {len(items)}",
        f"- Original tweets: {len(by_kind['tweet'])}",
        f"- Replies: {len(by_kind['reply'])}",
        f"- Note tweets: {len(by_kind['note'])}",
        f"- Community tweets: {len(by_kind['community'])}",
        "",
        "## Core Voice Summary",
        "",
        "The archive reads as thesis-first, builder-native, skeptical, and more interested in naming the real product or market dynamic than sounding polished.",
        "The writing usually pushes toward contrast, mechanism, or a sharper framing, but the better examples still feel typed in-feed rather than workshop-polished.",
        "",
        "## Observable Habits",
        "",
        f"- Original posts average about {round(mean(len(text) for text in originals), 1) if originals else 0.0} characters.",
        f"- Replies average about {reply_average} characters and are usually tighter, more direct, and more situational.",
        f"- Multiline originals are common: {multiline_posts} of {len(originals)}.",
        f"- Questions show up in {questions} original posts. Exclamation points are rare: {exclaims}.",
        f"- Emoji are uncommon: {emoji_posts} original posts contain emoji.",
        f"- Lowercase casual `i` is rare but real: {lowercase_i} posts.",
        "",
        "## Common Openings",
        "",
        f"- Most common first words: {', '.join(f'`{word}` ({count})' for word, count in first_words.most_common(8))}",
        f"- Common opening bigrams: {', '.join(f'`{phrase}` ({count})' for phrase, count in first_bigrams.most_common(10))}",
        "",
        "## Repeated Moves",
        "",
        "- Start with the claim, not a warm-up.",
        "- Reframe the category or call out the layer that actually matters.",
        "- Use contrast heavily: what people think vs what is structurally true.",
        "- Shift between blunt declarative lines and a slightly longer unpacking line.",
        "- Replies are usually direct answers, concrete asks, or one sharp observation tied to the source post.",
        "",
        "## Lexicon And Signals",
        "",
        f"- `actually` appears in {phrase_hits['actually']} deduped items.",
        f"- `genuinely` appears in {phrase_hits['genuinely']} deduped items.",
        f"- `most` appears in {phrase_hits['most']} deduped items.",
        f"- `everyone` appears in {phrase_hits['everyone']} deduped items.",
        f"- `nobody` appears in {phrase_hits['nobody']} deduped items.",
        f"- `product` appears in {phrase_hits['product']} deduped items.",
        f"- `builder` appears in {phrase_hits['builder']} deduped items.",
        f"- `memory` appears in {phrase_hits['memory']} deduped items.",
        f"- `continuity` appears in {phrase_hits['continuity']} deduped items.",
        f"- `presence` appears in {phrase_hits['presence']} deduped items.",
        f"- `trust` appears in {phrase_hits['trust']} deduped items.",
        f"- `the real story` is rare ({phrase_hits['the real story']}), so it should not be used as a default crutch.",
        f"- `wrong question` is rare ({phrase_hits['wrong question']}), so it should only appear when the source really earns it.",
        "",
        "## What This Means For Generation",
        "",
        "- Keep the voice sharp, but do not force a manifesto on every short post.",
        "- A stronger match is often one clear observation with a bit of roughness, not a perfect claim -> mechanism -> closer structure.",
        "- Preserve the archive's skepticism and product instinct without forcing every reply back into the same house themes.",
        "- Do not overuse dramatic reframes that are only occasional in the real archive.",
        "- Prefer real specificity from the source tweet over generic worldview flexing.",
        "",
        "## Representative Archive Excerpts",
        "",
    ]

    for excerpt in examples:
        lines.append(f"- {excerpt}")

    lines.extend(
        [
            "",
            "## Active Guardrails",
            "",
            "- Treat this as archive-derived evidence, not a complete identity file.",
            "- Use it to improve Voice.md proposals without touching Humanizer.md.",
            "- Keep WhoAmI.md factual and separate from archive tone analysis.",
        ]
    )
    return "\n".join(lines) + "\n"


def import_archive(archive_dir: Path) -> tuple[Path, list[dict[str, str]], str]:
    resolved = resolve_archive_dir(archive_dir)
    items = collect_archive_items(resolved)
    summary = render_archive_summary(resolved, items)
    return resolved, items, summary
