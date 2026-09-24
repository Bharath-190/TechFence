"""Classifier tests (kit Prompt B2): hardcoded positive/negative cases."""

from taskfence.classifier import classify

SID = "test-case"  # asset_id hint (no registry hit for this id)


def test_email_hits_pii():
    assert "PII" in classify(None, "reach me at bob@corp.invalid thanks")
    assert "PII" in classify(None, "x bob.smith@mail.example y")
    assert "PII" in classify(None, "a@b.test")


def test_email_non_hits():
    assert "PII" not in classify(None, "no address in this plain note")
    assert "PII" not in classify(None, "host names like EXAMPLE.COM only")
    assert "PII" not in classify(None, "version 1.2.3 released")


def test_indian_mobile_hits_pii():
    assert "PII" in classify(None, "call 9876543210 now")
    assert "PII" in classify(None, "alt line 8123456789")
    assert "PII" in classify(None, "9900112233 is registered")


def test_mobile_non_hits():
    assert "PII" not in classify(None, "amount 1234567890 units")
    assert "PII" not in classify(None, "invoice 123456789")
    assert "PII" not in classify(None, "short code 98765")


def test_aadhaar_and_pan_hit_pii():
    assert "PII" in classify(None, "aadhaar 2345 6789 0123 on file")
    assert "PII" in classify(None, "PAN ABCDE1234F verified")
    assert "PII" in classify(None, "id 9876 1234 5678 captured")


def test_id_non_hits():
    assert "PII" not in classify(None, "not 12 digits: 12345678901234")
    assert "PII" not in classify(None, "letters only ABDEFG")
    assert "PAN" not in classify(None, "pan card copy pending")


def test_inr_amounts_and_salary_keywords_hit_financial():
    assert "FINANCIAL" in classify(None, "cost ₹ 4,50,000 per year")
    assert "FINANCIAL" in classify(None, "INR 250000 budgeted")
    assert "FINANCIAL" in classify(None, "his salary is high")
    assert "FINANCIAL" in classify(None, "ctc bands were revised")


def test_financial_non_hits():
    assert "FINANCIAL" not in classify(None, "units shipped: 4,50,000")
    assert "₹" not in "no rupee amounts here"
    assert "FINANCIAL" not in classify(None, "plain meeting notes")


def test_api_key_and_code_markers_hit_source_code():
    assert "SOURCE_CODE" in classify(None, "key = sk-live-abcdef0123456789abcd")
    assert "SOURCE_CODE" in classify(None, "def broken():\n    pass")
    assert "SOURCE_CODE" in classify(None, "import os")


def test_source_code_non_hits():
    assert "SOURCE_CODE" not in classify(None, "the def scale review")
    assert "SOURCE_CODE" not in classify(None, "words like import and class")
    assert "SOURCE_CODE" not in classify(None, "plain prose paragraph")


def test_customer_record_shape_hits_customer_data():
    assert "CUSTOMER_DATA" in classify(None, "record CU-014 shows churn risk")
    assert "CUSTOMER_DATA" in classify(None, "CUST-7 profile")
    assert "CUSTOMER_DATA" in classify(None, "CU-030 and CU-031")


def test_customer_non_hits():
    assert "CUSTOMER_DATA" not in classify(None, "team meeting at 14:00")
    assert "CUSTOMER_DATA" not in classify(None, "version CU prefix talk")
    assert "CUSTOMER_DATA" not in classify(None, "route CU42 shortcut")


def test_unknown_content_defaults_to_internal():
    assert classify(None, "ordinary substantive prose") == {"INTERNAL"}


def test_public_asset_stays_public_only():
    assert classify("public_company_info", _load_public()) == {"PUBLIC"}


def test_registry_hint_labels_are_included():
    labels = classify("customer_db", "just a fragment")
    assert {"CUSTOMER_DATA", "PII", "FINANCIAL"} <= labels


def test_empty_text_defaults_to_internal():
    assert classify(None, "") == {"INTERNAL"}


def _load_public() -> str:
    from taskfence.registry import read_asset
    return read_asset("public_company_info")
