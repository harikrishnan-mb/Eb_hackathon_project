from assistant import verifier
from assistant.context import MAX_OBS_TOKENS, compress_observation

SRC = ["Returns are accepted within 30 days for SKU-001 Ceramic Mug. Standard returns are processed by the returns desk."]


def test_supported_sentences_are_kept():
    kept, removed = verifier.verify("Returns are accepted within 30 days for SKU-001 [note_returns].", SRC)
    assert removed == [] and "30 days" in kept


def test_unsupported_sentence_is_vetoed():
    kept, removed = verifier.verify(
        "Standard returns are processed by the returns desk. The mug ships in gold foil with a lifetime warranty.", SRC)
    assert removed == ["The mug ships in gold foil with a lifetime warranty."]
    assert "gold foil" not in kept


def test_all_unsupported_leaves_nothing():
    kept, removed = verifier.verify("Quantum drones deliver pizza to orbit nightly.", SRC)
    assert kept == "" and len(removed) == 1


def test_compression_caps_length_and_drops_headings():
    long = "# Title\n" + " ".join(["word"] * 500)
    out = compress_observation(long)
    assert len(out.split()) == MAX_OBS_TOKENS + 1  # cap plus the ellipsis token
    assert "Title" not in out
