"""Customer store unit tests — schema, CRUD, edge cases."""

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

from agent.customer_store import CustomerStore, _normalize_phone, _new_customer_id


@pytest.fixture
async def store(tmp_path):
    db_path = tmp_path / "customers.db"
    s = CustomerStore(db_path=db_path)
    await s.init()
    return s


def test_phone_normalization():
    assert _normalize_phone("+91 72407 34163") == "7240734163"
    assert _normalize_phone("917240734163")    == "7240734163"
    assert _normalize_phone("72-40-73-41-63")  == "7240734163"
    assert _normalize_phone("abc")             == "abc".replace("abc", "")
    # Short input passes through truncated
    assert len(_normalize_phone("9876"))       == 4


def test_customer_id_format():
    cid = _new_customer_id()
    assert cid.startswith("CUST-")
    assert len(cid) == 13                 # "CUST-" + 8
    assert cid[5:].isalnum()
    # No ambiguous characters (0, O, 1, I)
    for bad in "0O1I":
        assert bad not in cid[5:]


@pytest.mark.asyncio
async def test_customer_upsert_creates_and_dedupes(store):
    c1 = await store.upsert_customer(phone="9876512345", name="Aarav")
    c2 = await store.upsert_customer(phone="9876512345", name="Aarav")
    assert c1.customer_id == c2.customer_id
    # Normalisation: same phone in different format still dedupes
    c3 = await store.upsert_customer(phone="+91 98765-12345")
    assert c3.customer_id == c1.customer_id


@pytest.mark.asyncio
async def test_customer_upsert_backfills_name(store):
    c1 = await store.upsert_customer(phone="9876543210", name="")
    c2 = await store.upsert_customer(phone="9876543210", name="Backfilled")
    fetched = await store.get_customer(c1.customer_id)
    assert fetched.name == "Backfilled"


@pytest.mark.asyncio
async def test_invalid_phone_rejected(store):
    with pytest.raises(ValueError):
        await store.upsert_customer(phone="abc")
    with pytest.raises(ValueError):
        await store.upsert_customer(phone="")


@pytest.mark.asyncio
async def test_lookup_by_phone_and_id(store):
    c = await store.upsert_customer(phone="9000000001", name="X")
    by_phone = await store.get_customer_by_phone("+91 90000 00001")
    by_id    = await store.get_customer(c.customer_id)
    assert by_phone.customer_id == by_id.customer_id == c.customer_id
    # Unknown returns None, doesn't raise
    assert await store.get_customer_by_phone("0000000000") is None
    assert await store.get_customer("CUST-DOESNOTEX") is None


@pytest.mark.asyncio
async def test_application_lifecycle(store):
    c = await store.upsert_customer(phone="9111111111")
    a = await store.create_application(
        customer_id=c.customer_id, service_type="DL_RENEWAL",
        state_code="RJ", fee_inr=200, metadata={"dl": "RJ07X"},
    )
    assert a.status == "CREATED"
    assert a.metadata["dl"] == "RJ07X"

    a2 = await store.update_application(
        a.app_id, status="WAITING_OTP",
        metadata_patch={"otp_sent_at": "now"},
    )
    assert a2.status == "WAITING_OTP"
    assert a2.metadata["dl"] == "RJ07X"            # preserved
    assert a2.metadata["otp_sent_at"] == "now"     # patched

    # Unknown app_id -> None
    assert await store.update_application("APP-NONE", status="FAILED") is None
    assert await store.get_application("APP-NONE") is None


@pytest.mark.asyncio
async def test_list_applications_filters(store):
    c = await store.upsert_customer(phone="9222222222")
    a1 = await store.create_application(customer_id=c.customer_id, service_type="DL_RENEWAL")
    a2 = await store.create_application(customer_id=c.customer_id, service_type="DUPLICATE_DL")
    await store.update_application(a2.app_id, status="COMPLETED")

    only_renewal = await store.list_applications(service="DL_RENEWAL")
    assert len(only_renewal) == 1 and only_renewal[0]["app_id"] == a1.app_id

    only_done = await store.list_applications(status="COMPLETED")
    assert len(only_done) == 1 and only_done[0]["app_id"] == a2.app_id

    by_customer = await store.list_applications(customer_id=c.customer_id)
    assert len(by_customer) == 2


@pytest.mark.asyncio
async def test_documents_and_notes(store, tmp_path):
    c = await store.upsert_customer(phone="9333333333")
    a = await store.create_application(customer_id=c.customer_id, service_type="DL_RENEWAL")

    f = tmp_path / "dl.png"
    f.write_bytes(b"\x89PNG\r\n")

    d = await store.add_document(
        customer_id=c.customer_id, app_id=a.app_id,
        doc_type="driving_license", file_path=str(f),
        mime_type="image/png", ocr_data={"x": 1}, confidence=0.8,
    )
    assert d.size_bytes == f.stat().st_size
    assert d.ocr_data == {"x": 1}

    fetched = await store.get_document(d.doc_id)
    assert fetched.confidence == 0.8

    # Empty-text note rejected
    with pytest.raises(ValueError):
        await store.add_note(a.app_id, "   ")

    note = await store.add_note(a.app_id, "Looks good")
    assert note["note_id"] > 0
    notes = await store.list_notes(a.app_id)
    assert len(notes) == 1 and notes[0]["text"] == "Looks good"


@pytest.mark.asyncio
async def test_counts(store):
    c = await store.upsert_customer(phone="9444444444")
    await store.create_application(customer_id=c.customer_id, service_type="DL_RENEWAL")
    a2 = await store.create_application(customer_id=c.customer_id, service_type="DUPLICATE_DL")
    await store.update_application(a2.app_id, status="COMPLETED")
    counts = await store.counts()
    assert counts["customers"] == 1
    assert counts["applications"] == 2
    assert counts["by_status"]["COMPLETED"] == 1
