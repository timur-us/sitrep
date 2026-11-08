"""The chunker: at most CHUNK_TOKENS tokens per passage, the prefix included (ADR 0005).

With the model's own tokenizer (needs `make model`): every document of the generated
corpus is one passage that fits, and long texts, in German, English and shapes a
tokenizer finds awkward, become passages that each fit, are verbatim slices, run end
to end, and are not cut short without need. With a word tokenizer of the test's own:
where the cuts fall.
"""

from __future__ import annotations

import json
import re

import pytest

from app.config.embedding import CHUNK_TOKENS, MAX_TOKENS
from app.embeddings.base import passage_input
from app.embeddings.chunking import chunk
from app.embeddings.onnx import ModelTokenizer
from data.synthetic import generate


class Words:
    """A tokenizer where each run of non-space characters is a token, plus two specials."""

    def count(self, text: str) -> int:
        return len(text.split()) + 2

    def offsets(self, text: str) -> list[tuple[int, int]]:
        return [match.span() for match in re.finditer(r"\S+", text)]


def check(text: str, passages: list[str], tokenizer: ModelTokenizer | Words, limit: int) -> None:
    """Each passage fits, is a stripped slice, in order, and together they cover text."""
    assert all(passages), "an empty passage"
    position = 0
    for passage in passages:
        assert tokenizer.count(passage_input(passage)) <= limit, passage[:40]
        assert passage == passage.strip()
        found = text.find(passage, position)
        assert found >= position and not text[position:found].strip(), "not verbatim, in order"
        position = found + len(passage)
    assert not text[position:].strip(), "text left after the last passage"
    assert "".join("".join(passages).split()) == "".join(text.split())


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> list[dict[str, str]]:
    out = tmp_path_factory.mktemp("corpus")
    assert generate.main(["--out", str(out)]) == 0
    rows = []
    for name in ("documents.jsonl", "product_sheets.jsonl"):
        with (out / name).open(encoding="utf-8") as lines:
            rows += [json.loads(line) for line in lines]
    return rows


def test_every_corpus_document_is_one_passage(
    corpus: list[dict[str, str]], tokenizer: ModelTokenizer
) -> None:
    # Non-vacuity: the whole corpus, both languages. Its longest document counts about
    # 170 tokens, so none needs a cut; the tests below cut.
    assert len(corpus) == 350
    assert {row["lang"] for row in corpus} == {"german", "english"}
    for row in corpus:
        for text in (row["body"], f"{row['title']}\n\n{row['body']}"):
            assert chunk(text, tokenizer) == [text.strip()], row["id"]
            assert tokenizer.count(passage_input(text.strip())) <= CHUNK_TOKENS


def joined(corpus: list[dict[str, str]], lang: str) -> str:
    return "\n\n".join(f"{r['title']}\n\n{r['body']}" for r in corpus if r["lang"] == lang)


AWKWARD = {
    "no whitespace": "x" * 5000,
    "CJK": "南瓜的家常做法" * 400,
    "a character NFKC expands to 18": "\ufdfa" * 800,
    "zero-width separators": "Anleihe\u200b" * 900,
    "emoji": "💶 " * 900,
    "tabs and double spaces": "Anleihen  \t Termin " * 400,
    "line breaks only": "Zeile\n" * 900,
}


@pytest.mark.parametrize("lang", ["german", "english"])
def test_long_documents_are_cut_to_fit(
    corpus: list[dict[str, str]], tokenizer: ModelTokenizer, lang: str
) -> None:
    text = joined(corpus, lang)
    assert tokenizer.count(text) > 3 * CHUNK_TOKENS
    passages = chunk(text, tokenizer)
    check(text, passages, tokenizer, CHUNK_TOKENS)
    # A cut lies in the second half of its window, so no passage but the last is short.
    counts = [tokenizer.count(passage_input(passage)) for passage in passages]
    assert min(counts[:-1]) >= CHUNK_TOKENS // 2, counts
    # The corpus separates documents and paragraphs by blank lines: every cut takes one.
    position = 0
    for passage in passages[:-1]:
        position = text.find(passage, position) + len(passage)
        assert text[position:].startswith("\n\n"), repr(text[position : position + 20])


@pytest.mark.parametrize("case", AWKWARD, ids=str)
def test_awkward_texts_are_cut_to_fit(tokenizer: ModelTokenizer, case: str) -> None:
    text = AWKWARD[case]
    passages = chunk(text, tokenizer)
    assert len(passages) > 1
    check(text, passages, tokenizer, CHUNK_TOKENS)


def test_sentences_are_cut_at_their_end(tokenizer: ModelTokenizer) -> None:
    text = " ".join(
        f"Satz {n} beschreibt die Anlagestrategie des Kunden genau." for n in range(150)
    )
    passages = chunk(text, tokenizer)
    check(text, passages, tokenizer, CHUNK_TOKENS)
    assert len(passages) > 2 and all(passage.endswith(".") for passage in passages)


def test_the_limit_is_exact(tokenizer: ModelTokenizer) -> None:
    words = CHUNK_TOKENS - tokenizer.count(passage_input("a")) + 1
    fits = " ".join(["a"] * words)
    assert tokenizer.count(passage_input(fits)) == CHUNK_TOKENS
    assert chunk(fits, tokenizer) == [fits]
    over = chunk(fits + " a", tokenizer)
    assert len(over) == 2
    check(fits + " a", over, tokenizer, CHUNK_TOKENS)


def test_the_limit_is_below_the_model_cap() -> None:
    assert CHUNK_TOKENS == 400 and CHUNK_TOKENS < MAX_TOKENS


@pytest.mark.parametrize("text", ["", " ", "\n\n\t ", "\u3000"])
def test_nothing_to_embed(text: str, tokenizer: ModelTokenizer) -> None:
    assert chunk(text, tokenizer) == []


def test_a_short_text_is_one_stripped_passage(tokenizer: ModelTokenizer) -> None:
    assert chunk("  Anleihen 40 %\n", tokenizer) == ["Anleihen 40 %"]


# With Words: "passage:" is one word, so a passage of n words counts n + 3.
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a b c d e f g h i j", ["a b c d e f g", "h i j"]),
        # A blank line in the second half of the window wins over every space.
        ("a b c\n\nd e f g h i j k", ["a b c", "d e f g h i j", "k"]),
        # A sentence end beats a space, a line break beats a sentence end.
        ("a b c d. e f g h i j", ["a b c d.", "e f g h i j"]),
        ("a b c. d\ne f g h i j", ["a b c. d", "e f g h i j"]),
        # A boundary in the first half is not taken: the passage would be short.
        ("a\n\nb c d e f g h i j", ["a\n\nb c d e f g", "h i j"]),
        # Whitespace inside a passage stays as it was.
        ("a  b\tc d e f g h i j", ["a  b\tc d e f g", "h i j"]),
    ],
)
def test_where_the_cuts_fall(text: str, expected: list[str]) -> None:
    passages = chunk(text, Words(), limit=10)
    assert passages == expected
    check(text, passages, Words(), 10)


@pytest.mark.parametrize("text", [b"Anleihen", None, ["Anleihen"], 400])
def test_refuses_what_is_not_text(text: object) -> None:
    with pytest.raises(TypeError):
        chunk(text, Words())  # type: ignore[arg-type]


@pytest.mark.parametrize("limit", [0, -1, MAX_TOKENS + 1, "400", 400.0, True, 3])
def test_refuses_a_limit_out_of_range(limit: object) -> None:
    # 3 leaves no room: Words counts "passage: " as 3 tokens.
    with pytest.raises(ValueError):
        chunk("a b c", Words(), limit=limit)  # type: ignore[arg-type]


def test_refuses_offsets_that_do_not_advance() -> None:
    class Stuck(Words):
        def offsets(self, text: str) -> list[tuple[int, int]]:
            return [(0, 1)] * len(text.split())

    with pytest.raises(ValueError, match="offsets"):
        chunk("a b c d e f g h i j k l", Stuck(), limit=10)
