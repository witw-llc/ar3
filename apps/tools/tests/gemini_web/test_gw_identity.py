from gemini_web import identity


def test_both_names_are_substituted_and_nothing_is_left_templated():
    text = identity.preamble("seat", "example-sender")
    assert "{" not in text and "}" not in text
    assert "seat" in text
    assert text.count("example-sender") >= 2


def test_it_says_the_three_things_a_conversation_needs_to_know():
    text = identity.preamble("seat", "example-sender").lower()
    # who it is, that a relay carries the messages, and who it is talking to
    assert "you are seat" in text
    assert "relay" in text
    assert "delivered" in text


def test_it_stays_short_enough_to_be_a_first_message():
    text = identity.preamble("seat", "example-sender")
    assert len(text) < 1200
    assert text.endswith("\n")


def test_the_module_carries_no_names_of_its_own():
    """The preamble ships in a public repo and is read by whoever is addressed;
    every name in it arrives at render time."""
    assert "{agent}" in identity.PREAMBLE
    assert "{correspondent}" in identity.PREAMBLE
