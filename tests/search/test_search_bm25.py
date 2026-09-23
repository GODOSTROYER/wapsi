import math

import pytest

from wapsi.search.bm25 import BM25, K1, STOPWORDS, B, tokenize

CORPUS = {
    "hold": "The bank nodal officer shall put the disputed amount on hold in the first-layer account.",
    "freeze": "Unwarranted orders for freezing accounts shall be discouraged.",
    "restore": "Interim custody of the amount put on hold is given to the victim under section 106(3) BNSS.",
    "merchant": "The merchant shall cancel the order if goods are not delivered and hold the amount in the pool account.",
}


def _index():
    ids = list(CORPUS)
    return ids, BM25([tokenize(CORPUS[i]) for i in ids])


def test_defaults_are_okapi_k1_b():
    assert (K1, B) == (1.5, 0.75)
    _, bm = _index()
    assert (bm.k1, bm.b) == (1.5, 0.75)


def test_ranking_prefers_documents_with_rare_query_terms():
    ids, bm = _index()
    ranked = [ids[i] for i, _ in bm.top_k(tokenize("first layer disputed amount hold"), k=4)]
    assert ranked[0] == "hold"
    ranked = [ids[i] for i, _ in bm.top_k(tokenize("unwarranted freezing of accounts"), k=4)]
    assert ranked[0] == "freeze"
    ranked = [ids[i] for i, _ in bm.top_k(tokenize("interim custody victim s. 106"), k=4)]
    assert ranked[0] == "restore"


def test_top_k_drops_zero_scores_and_respects_k():
    ids, bm = _index()
    assert bm.top_k(tokenize("cryptocurrency exchange"), k=3) == []
    assert len(bm.top_k(tokenize("amount"), k=2)) == 2
    assert bm.top_k(tokenize("amount"), k=0) == []


def test_idf_is_positive_and_decreases_with_document_frequency():
    _, bm = _index()
    assert bm.idf("amount") > 0  # appears in 3 of 4 documents
    assert bm.idf("unwarrant") > bm.idf("amount")
    n, df = 4, 1
    assert bm.idf("unwarrant") == pytest.approx(math.log(1 + (n - df + 0.5) / (df + 0.5)))


def test_length_normalisation_favours_shorter_document_with_same_tf():
    bm = BM25([["hold", "amount"], ["hold"] + ["filler"] * 20])
    scores = bm.scores(["hold"])
    assert scores[0] > scores[1]


def test_score_matches_formula():
    docs = [["hold", "hold", "amount"], ["victim"]]
    bm = BM25(docs)
    tf, dl, avgdl = 2, 3, 2.0
    expected = bm.idf("hold") * tf * (K1 + 1) / (tf + K1 * (1 - B + B * dl / avgdl))
    assert bm.score(["hold"], 0) == pytest.approx(expected)
    assert bm.score(["hold", "hold"], 0) == pytest.approx(expected)  # distinct query terms count once


def test_empty_index_is_safe():
    bm = BM25([])
    assert bm.top_k(["hold"], 5) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("s. 106 BNSS", ["section", "106", "bnss"]),
        ("Sec. 94", ["section", "94"]),
        ("u/s 106(3)", ["section", "106", "3"]),
        ("S.168 r/w S.94", ["section", "168", "read", "section", "94"]),
        ("Sections 497", ["section", "497"]),
        ("Bharatiya Nagarik Suraksha Sanhita", ["bnss"]),
        ("B.N.S.S.", ["bnss"]),
        ("P.M.L.A.", ["pmla"]),
        ("PML Act", ["pmla"]),
        ("Prevention of Money-laundering Act", ["pmla"]),
        ("Cr.P.C.", ["crpc"]),
        ("A/c No.", ["account"]),
        ("Rs. 50,000", ["rs", "50000"]),
        ("₹ 7,647", ["rs", "7647"]),
        ("tipping off", ["tipoff"]),
        ("first-layer", ["first", "layer", "firstlayer"]),
    ],
)
def test_tokenizer_normalises_legal_abbreviations(text, expected):
    assert tokenize(text) == expected


def test_tokenizer_lowercases_drops_stopwords_and_stems():
    assert tokenize("The HOLDS on the Accounts of Victims") == ["hold", "account", "victim"]
    assert tokenize("holding") == tokenize("hold") == tokenize("holds")
    assert tokenize("freezing") == tokenize("freeze")
    assert tokenize("entities") == ["entity"]
    assert tokenize("process processes processing") == ["process"] * 3
    assert "shall" in STOPWORDS and "the" in STOPWORDS
    assert tokenize("a") == []


def test_rs_prefix_does_not_look_like_section():
    assert "section" not in tokenize("Rs. 5,000 was held")
