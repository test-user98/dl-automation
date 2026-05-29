from tools.ocr_service import OCRService


def test_dl_assessment_treats_only_dl_number_and_dob_as_required():
    assessment = OCRService()._normalise_dl_assessment(
        {
            "accepted": False,
            "is_driving_license": True,
            "document_type": "driving_license",
            "image_quality": "clear",
            "confidence": 0.88,
            "rejection_reason": "missing_required",
            "extracted": {
                "dl_number": "RJ07 2017 0010191",
                "dob": "01-01-1990",
                "name": "Aarav Sharma",
            },
            "missing_fields": [
                "address",
                "pin_code",
                "vehicle_classes",
                "gender",
                "badge_number",
            ],
        }
    )

    assert assessment["accepted"] is True
    assert assessment["rejection_reason"] == ""
    assert assessment["missing_fields"] == []
    assert assessment["optional_missing_fields"] == [
        "address",
        "pin_code",
        "vehicle_classes",
        "gender",
        "badge_number",
    ]
    assert assessment["needs_manual_review"] is False


def test_dl_assessment_does_not_leak_required_aliases_into_optional_missing():
    assessment = OCRService()._normalise_dl_assessment(
        {
            "is_driving_license": True,
            "document_type": "driving_license",
            "confidence": 0.9,
            "extracted": {"name": "Aarav Sharma"},
            "missing_fields": ["dl_number", "dob", "address"],
        }
    )

    assert assessment["accepted"] is False
    assert assessment["rejection_reason"] == "missing_required"
    assert assessment["missing_fields"] == ["DL number", "date of birth"]
    assert assessment["optional_missing_fields"] == ["address"]
