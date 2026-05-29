"""Seed realistic mock data so the operator dashboard isn't empty in demo.

Runs at startup. Idempotent — if the customers table already has rows, it
does nothing.
"""

from __future__ import annotations

from pathlib import Path

from agent.customer_store import CustomerStore


_DEMO_CUSTOMERS = [
    {
        "phone": "9876512345", "name": "Aarav Mehta", "email": "aarav.mehta@example.in",
        "applications": [
            {
                "service": "DL_RENEWAL", "state_code": "RJ", "fee_inr": 200,
                "status": "SUBMITTED", "application_number": "RJ-DL-2026-04219",
                "metadata": {"dl_number": "RJ0720170010191", "dob": "04-09-1998",
                             "pin_code": "334401", "rto_code": "RJ07"},
                "documents": [
                    {"doc_type": "driving_license",
                     "ocr_data": {"dl_number": "RJ0720170010191", "name": "AARAV MEHTA",
                                  "dob": "04-09-1998", "expiry_date": "03-09-2025",
                                  "vehicle_classes": ["LMV", "MCWG"]},
                     "confidence": 0.94},
                ],
            },
        ],
    },
    {
        "phone": "9123456780", "name": "Sneha Iyer", "email": "",
        "applications": [
            {
                "service": "DL_RENEWAL", "state_code": "TN", "fee_inr": 200,
                "status": "WAITING_OTP", "application_number": "",
                "metadata": {"dl_number": "TN0120190034512", "dob": "12-06-1991",
                             "pin_code": "600041", "rto_code": "TN01"},
                "documents": [
                    {"doc_type": "driving_license",
                     "ocr_data": {"dl_number": "TN0120190034512", "name": "SNEHA IYER",
                                  "dob": "12-06-1991", "expiry_date": "11-06-2024"},
                     "confidence": 0.71},
                ],
            },
        ],
    },
    {
        "phone": "9988776655", "name": "Rohit Verma", "email": "rohit.v@example.in",
        "applications": [
            {
                "service": "DL_RENEWAL", "state_code": "DL", "fee_inr": 200,
                "status": "AGENT_RUNNING",
                "metadata": {"dl_number": "DL0420110012345", "dob": "23-11-1985",
                             "pin_code": "110024", "rto_code": "DL04"},
                "documents": [
                    {"doc_type": "driving_license",
                     "ocr_data": {"dl_number": "DL0420110012345", "name": "ROHIT VERMA",
                                  "dob": "23-11-1985"},
                     "confidence": 0.62},
                ],
            },
            {
                "service": "DUPLICATE_DL", "state_code": "DL", "fee_inr": 300,
                "status": "COMPLETED", "application_number": "DL-DUP-2026-12044",
                "metadata": {"dl_number": "DL0420110012345"},
                "documents": [],
            },
        ],
    },
    {
        "phone": "9090909090", "name": "Priya Sharma", "email": "",
        "applications": [
            {
                "service": "CHANGE_OF_ADDRESS", "state_code": "MH", "fee_inr": 200,
                "status": "STUCK_HUMAN_NEEDED",
                "metadata": {"dl_number": "MH1220150089231", "dob": "07-02-1990",
                             "pin_code": "400053"},
                "documents": [
                    {"doc_type": "driving_license",
                     "ocr_data": {"dl_number": "MH1220150089231", "name": "PRIYA SHARMA"},
                     "confidence": 0.55},
                    {"doc_type": "address_proof",
                     "ocr_data": {"address": "B-403 Aspire Heights, Andheri West, Mumbai 400053"},
                     "confidence": 0.88},
                ],
            },
        ],
    },
    {
        "phone": "7000123456", "name": "Karthik Reddy", "email": "karthik@example.in",
        "applications": [
            {
                "service": "DL_RENEWAL", "state_code": "KA", "fee_inr": 200,
                "status": "FAILED",
                "metadata": {"dl_number": "KA0520180067891", "dob": "30-05-1989",
                             "pin_code": "560001"},
                "documents": [
                    {"doc_type": "driving_license",
                     "ocr_data": {"dl_number": "KA0520180067891"},
                     "confidence": 0.41},
                ],
            },
        ],
    },
]


async def seed_if_empty(store: CustomerStore) -> dict:
    """Insert demo data only if no customers exist. Returns a small report."""
    counts = await store.counts()
    if counts["customers"] > 0:
        return {"seeded": False, "reason": "already populated", "counts": counts}

    created = {"customers": 0, "applications": 0, "documents": 0, "notes": 0}
    sample_dir = Path("data") / "seed_samples"
    sample_dir.mkdir(parents=True, exist_ok=True)
    placeholder = sample_dir / "sample.png"
    if not placeholder.exists():
        # 1x1 transparent PNG — enough for size_bytes + the preview endpoint
        placeholder.write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xcf"
            b"\xc0\xf0\x1f\x00\x05\x00\x01\xff?\x12\x99\xc4\x00\x00\x00\x00IEND\xaeB`\x82"
        )

    for cust in _DEMO_CUSTOMERS:
        c = await store.upsert_customer(phone=cust["phone"], name=cust["name"], email=cust.get("email", ""))
        created["customers"] += 1
        for app in cust.get("applications", []):
            a = await store.create_application(
                customer_id=c.customer_id,
                service_type=app["service"],
                state_code=app.get("state_code", ""),
                fee_inr=app.get("fee_inr", 0),
                metadata=app.get("metadata", {}),
            )
            created["applications"] += 1
            if app.get("status") and app["status"] != "CREATED":
                await store.update_application(
                    a.app_id,
                    status=app["status"],
                    application_number=app.get("application_number", ""),
                )
            for doc in app.get("documents", []):
                await store.add_document(
                    customer_id=c.customer_id,
                    app_id=a.app_id,
                    doc_type=doc["doc_type"],
                    file_path=str(placeholder),
                    mime_type="image/png",
                    ocr_data=doc.get("ocr_data", {}),
                    confidence=doc.get("confidence", 0.0),
                )
                created["documents"] += 1

    # One sample operator note so the UI shows the note thread
    apps = await store.list_applications(status="SUBMITTED", limit=1)
    if apps:
        await store.add_note(apps[0]["app_id"], "Acknowledgement downloaded from Sarathi.", "operator")
        created["notes"] += 1

    return {"seeded": True, "created": created}
