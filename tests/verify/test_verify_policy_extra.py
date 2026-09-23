from wapsi.verify.policy import freeze_instructions, suspension_instructions


def test_legal_requirement_statements_are_not_freeze_instructions():
    assert (
        freeze_instructions(
            "Seizure under Section 106 BNSS needs a registered FIR, and a debit freeze of the account needs an "
            "order of the Magistrate under Section 107 BNSS [BNSS-106] [BNSS-107]"
        )
        == []
    )
    assert freeze_instructions("A debit freeze requires a Magistrate's order under Section 107 BNSS.") == []


def test_real_freeze_instructions_still_caught():
    assert freeze_instructions("Freeze the entire account HB01-1000000001 immediately.")
    assert freeze_instructions("Please place a debit freeze on the account until further orders.")


def test_suspension_and_seizure_instructions_are_caught():
    for text in (
        "Suspend digital banking for the pool account.",
        "Please seize the account HB01-9000000001.",
        "Seize the pool account and block UPI.",
        "Suspend the account immediately.",
        "Suspension of internet banking services on the nodal account with immediate effect.",
        "Do not delay, seize the account.",
    ):
        assert suspension_instructions(text), text


def test_negated_or_descriptive_suspension_mentions_are_allowed():
    for text in (
        "Do not suspend digital banking or seize the account; operations for other customers' funds continue "
        "[SOP-9-1-III].",
        "Do not suspend digital banking services on the pool account or seize the account.",
        "Neither suspend UPI nor seize the funds.",
        "LEAs should normally refrain from ordering suspension of digital banking services or seizure of such "
        "accounts.",
        "Suspension of digital banking services is not warranted.",
        "Seizure of the account needs an FIR.",
        "Seizure under Section 106 BNSS needs a registered FIR, and a debit freeze of the account needs an order "
        "of the Magistrate under Section 107 BNSS [BNSS-106] [BNSS-107]",
        "The pool account is not suspended or seized.",
        "Hold only the disputed amount in this nodal / pool / escrow account.",
    ):
        assert suspension_instructions(text) == [], text
